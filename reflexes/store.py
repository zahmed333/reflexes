"""Durable state for Reflexes: rules, the event queue, folder baselines and the detector lease.

Everything lives in one SQLite file in the plugin's data directory, so every Hermes process that
loads the plugin (gateway, classic CLI, a cron run) sees the same rules and the same queue. One
process detects (it holds the lease); whichever process hosts a rule's session delivers.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    config TEXT NOT NULL,
    prompt TEXT NOT NULL,
    session_key TEXT NOT NULL DEFAULT '',
    surface TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    paused_reason TEXT NOT NULL DEFAULT '',
    health TEXT NOT NULL DEFAULT '',
    max_per_hour INTEGER NOT NULL DEFAULT 30,
    created_at REAL NOT NULL,
    fire_count INTEGER NOT NULL DEFAULT 0,
    last_fired_at REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    created_at REAL NOT NULL,
    summary TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    claimed_by TEXT NOT NULL DEFAULT '',
    claimed_at REAL,
    delivered_at REAL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_status ON events(status, created_at);
CREATE TABLE IF NOT EXISTS seen (
    rule_id TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    PRIMARY KEY (rule_id, path)
);
CREATE TABLE IF NOT EXISTS baselined (rule_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS agent_touch (path TEXT PRIMARY KEY, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (key TEXT PRIMARY KEY, surface TEXT NOT NULL DEFAULT '', last_seen REAL NOT NULL);
"""

# Event lifecycle: pending -> delivering -> delivered, or pending -> missed (nobody could take it
# before the TTL), or suppressed (rate limit), recorded so the log explains every non-delivery.
PENDING, DELIVERING, DELIVERED, MISSED, SUPPRESSED = "pending", "delivering", "delivered", "missed", "suppressed"

_NAME_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    return _NAME_RE.sub("-", (text or "").lower()).strip("-")[:40] or "reflex"


def _log_key(rule_id: str) -> str:
    return f"log-offset:{rule_id}"


def default_db_path() -> Path:
    """<HERMES_HOME>/plugin-data/reflexes/reflexes.db (profile-scoped, survives plugin updates)."""
    from plugins.plugin_storage import plugin_data_dir

    return Path(plugin_data_dir("reflexes")) / "reflexes.db"


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._local = threading.local()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn().executescript(_SCHEMA)  # idempotent; executescript manages its own transaction

    # -- connections -----------------------------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def _tx(self) -> "_Tx":
        return _Tx(self._conn())

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- rules -----------------------------------------------------------------------------------
    def add_rule(self, *, name: str, kind: str, config: dict, prompt: str, session_key: str = "",
                 surface: str = "", max_per_hour: int = 30, now: Optional[float] = None) -> dict:
        base = slugify(name)
        with self._tx() as db:
            taken = {r["name"] for r in db.execute("SELECT name FROM rules")}
            final, n = base, 2
            while final in taken:
                final, n = f"{base}-{n}", n + 1
            rule_id = uuid.uuid4().hex[:12]
            db.execute(
                "INSERT INTO rules (id, name, kind, config, prompt, session_key, surface, max_per_hour, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (rule_id, final, kind, json.dumps(config), prompt, session_key or "", surface or "",
                 int(max_per_hour), now if now is not None else time.time()),
            )
        return self.get_rule(rule_id)  # type: ignore[return-value]

    def get_rule(self, ref: str) -> Optional[dict]:
        """Look a rule up by id or by name."""
        row = self._conn().execute("SELECT * FROM rules WHERE id=? OR name=?", (ref, slugify(ref))).fetchone()
        return _rule(row)

    def list_rules(self, *, enabled_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM rules" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY created_at"
        return [_rule(r) for r in self._conn().execute(sql)]  # type: ignore[misc]

    def count_rules(self) -> int:
        return int(self._conn().execute("SELECT COUNT(*) FROM rules WHERE enabled=1").fetchone()[0])

    def set_enabled(self, rule_id: str, enabled: bool, reason: str = "") -> None:
        with self._tx() as db:
            db.execute("UPDATE rules SET enabled=?, paused_reason=? WHERE id=?",
                       (1 if enabled else 0, "" if enabled else reason, rule_id))
            if enabled:  # what arrived while paused is not news: look at the folder afresh
                db.execute("DELETE FROM baselined WHERE rule_id=?", (rule_id,))
                db.execute("DELETE FROM seen WHERE rule_id=?", (rule_id,))
                db.execute("DELETE FROM meta WHERE key=?", (_log_key(rule_id),))

    def set_health(self, rule_id: str, health: str) -> None:
        with self._tx() as db:
            db.execute("UPDATE rules SET health=? WHERE id=? AND health<>?", (health, rule_id, health))

    def bind_session(self, rule_id: str, session_key: str, surface: str = "") -> None:
        with self._tx() as db:
            db.execute("UPDATE rules SET session_key=?, surface=? WHERE id=?", (session_key or "", surface or "", rule_id))

    def remove_rule(self, rule_id: str) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM rules WHERE id=?", (rule_id,))
            db.execute("DELETE FROM seen WHERE rule_id=?", (rule_id,))
            db.execute("DELETE FROM baselined WHERE rule_id=?", (rule_id,))
            db.execute("DELETE FROM meta WHERE key=?", (_log_key(rule_id),))
            db.execute("UPDATE events SET status=?, detail='reflex removed' WHERE rule_id=? AND status=?",
                       (MISSED, rule_id, PENDING))

    # -- folder baselines ------------------------------------------------------------------------
    def load_seen(self, rule_id: str) -> Optional[dict[str, tuple[int, int]]]:
        """The folder contents this rule already knows about, or None if it was never baselined."""
        db = self._conn()
        if db.execute("SELECT 1 FROM baselined WHERE rule_id=?", (rule_id,)).fetchone() is None:
            return None
        return {r["path"]: (r["size"], r["mtime_ns"])
                for r in db.execute("SELECT path, size, mtime_ns FROM seen WHERE rule_id=?", (rule_id,))}

    def save_seen(self, rule_id: str, upserts: dict[str, tuple[int, int]], removals: Iterable[str] = ()) -> None:
        with self._tx() as db:
            db.execute("INSERT OR IGNORE INTO baselined (rule_id) VALUES (?)", (rule_id,))
            db.executemany("INSERT OR REPLACE INTO seen (rule_id, path, size, mtime_ns) VALUES (?,?,?,?)",
                           [(rule_id, p, s, m) for p, (s, m) in upserts.items()])
            db.executemany("DELETE FROM seen WHERE rule_id=? AND path=?", [(rule_id, p) for p in removals])

    # -- log-file read positions (kept in ``meta`` so no schema change is needed) -------------------
    def load_log_offset(self, rule_id: str) -> Optional[int]:
        row = self._conn().execute("SELECT value FROM meta WHERE key=?", (_log_key(rule_id),)).fetchone()
        try:
            return max(0, int(row["value"])) if row is not None else None
        except (ValueError, TypeError):
            return None

    def save_log_offset(self, rule_id: str, offset: int) -> None:
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (_log_key(rule_id), str(int(offset))))

    # -- paths the agent itself just wrote (so a reflex does not fire on its own output) ----------
    def touch(self, paths: Iterable[str], now: Optional[float] = None) -> None:
        ts = now if now is not None else time.time()
        with self._tx() as db:
            db.executemany("INSERT OR REPLACE INTO agent_touch (path, ts) VALUES (?,?)", [(p, ts) for p in paths])
            db.execute("DELETE FROM agent_touch WHERE ts < ?", (ts - 3600,))

    def touched_since(self, since: float) -> set[str]:
        return {r["path"] for r in self._conn().execute("SELECT path FROM agent_touch WHERE ts >= ?", (since,))}

    # -- events ----------------------------------------------------------------------------------
    def add_event(self, rule: dict, summary: str, payload: dict, *, status: str = PENDING,
                  detail: str = "", now: Optional[float] = None) -> int:
        ts = now if now is not None else time.time()
        with self._tx() as db:
            cur = db.execute(
                "INSERT INTO events (rule_id, rule_name, created_at, summary, payload, status, detail) VALUES (?,?,?,?,?,?,?)",
                (rule["id"], rule["name"], ts, summary, json.dumps(payload), status, detail),
            )
            if status == PENDING:
                db.execute("UPDATE rules SET fire_count=fire_count+1, last_fired_at=? WHERE id=?", (ts, rule["id"]))
            return int(cur.lastrowid)

    def fires_since(self, rule_id: str, since: float) -> int:
        return int(self._conn().execute(
            "SELECT COUNT(*) FROM events WHERE rule_id=? AND created_at>=? AND status<>?",
            (rule_id, since, SUPPRESSED)).fetchone()[0])

    def pending_events(self, limit: int = 20) -> list[dict]:
        rows = self._conn().execute(
            "SELECT e.*, r.session_key AS session_key, r.prompt AS prompt FROM events e"
            " JOIN rules r ON r.id = e.rule_id WHERE e.status=? ORDER BY e.id LIMIT ?", (PENDING, limit))
        return [_event(r) for r in rows]

    def claim(self, event_id: int, owner: str, now: Optional[float] = None) -> bool:
        with self._tx() as db:
            cur = db.execute("UPDATE events SET status=?, claimed_by=?, claimed_at=?, attempts=attempts+1"
                             " WHERE id=? AND status=?",
                             (DELIVERING, owner, now if now is not None else time.time(), event_id, PENDING))
            return cur.rowcount == 1

    def finish(self, event_id: int, delivered: bool, detail: str = "", now: Optional[float] = None) -> None:
        with self._tx() as db:
            if delivered:
                db.execute("UPDATE events SET status=?, delivered_at=?, detail=? WHERE id=?",
                           (DELIVERED, now if now is not None else time.time(), detail, event_id))
            else:
                db.execute("UPDATE events SET status=?, claimed_by='', detail=? WHERE id=? AND status=?",
                           (PENDING, detail, event_id, DELIVERING))

    def annotate_pending(self, detail: str) -> None:
        """Record why waiting events are not moving, so ``/reflex log`` can say so."""
        with self._tx() as db:
            db.execute("UPDATE events SET detail=? WHERE status=? AND detail<>?", (detail, PENDING, detail))

    def expire(self, ttl: float, now: Optional[float] = None) -> int:
        """Give up on events nobody could deliver, and release claims a crashed process left behind."""
        ts = now if now is not None else time.time()
        with self._tx() as db:
            db.execute("UPDATE events SET status=?, claimed_by='' WHERE status=? AND claimed_at < ?",
                       (PENDING, DELIVERING, ts - 30))
            cur = db.execute(
                "UPDATE events SET status=?, detail='no open Hermes session could take it before it expired'"
                " WHERE status=? AND created_at < ?", (MISSED, PENDING, ts - ttl))
            db.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 500)")
            return cur.rowcount

    def recent_events(self, limit: int = 20, rule_id: Optional[str] = None) -> list[dict]:
        sql, args = "SELECT * FROM events", []
        if rule_id:
            sql, args = sql + " WHERE rule_id=?", [rule_id]
        rows = self._conn().execute(sql + " ORDER BY id DESC LIMIT ?", (*args, int(limit)))
        return [_event(r) for r in rows]

    # -- chats seen by this plugin (route keys come from Hermes; they are never constructed here) --
    def see_session(self, key: str, surface: str = "", now: Optional[float] = None) -> None:
        if not key:
            return
        ts = now if now is not None else time.time()
        with self._tx() as db:
            db.execute("INSERT INTO sessions (key, surface, last_seen) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE"
                       " SET last_seen=excluded.last_seen, surface=CASE WHEN excluded.surface<>'' THEN excluded.surface"
                       " ELSE sessions.surface END", (key, surface or "", ts))
            db.execute("DELETE FROM sessions WHERE key NOT IN (SELECT key FROM sessions ORDER BY last_seen DESC LIMIT 30)")

    def recent_sessions(self, limit: int = 10) -> list[dict]:
        rows = self._conn().execute("SELECT * FROM sessions ORDER BY last_seen DESC LIMIT ?", (int(limit),))
        return [dict(r) for r in rows]

    def lease_age(self, now: Optional[float] = None) -> Optional[float]:
        """Seconds since some process last renewed the detector lease, None if nobody ever has."""
        row = self._conn().execute("SELECT value FROM meta WHERE key='lease'").fetchone()
        if row is None:
            return None
        try:
            return (now if now is not None else time.time()) - float(json.loads(row["value"]).get("ts", 0))
        except (ValueError, TypeError):
            return None

    # -- detector lease --------------------------------------------------------------------------
    def hold_lease(self, owner: str, ttl: float = 5.0, now: Optional[float] = None) -> bool:
        """True while ``owner`` is the one process allowed to detect. Taken only when the previous
        holder stopped renewing, so a short-lived command never steals it from a running gateway."""
        ts = now if now is not None else time.time()
        with self._tx() as db:
            row = db.execute("SELECT value FROM meta WHERE key='lease'").fetchone()
            holder, seen_at = "", 0.0
            if row is not None:
                try:
                    data = json.loads(row["value"])
                    holder, seen_at = str(data.get("owner", "")), float(data.get("ts", 0))
                except (ValueError, TypeError):
                    pass
            if holder and holder != owner and ts - seen_at < ttl:
                return False
            db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('lease', ?)",
                       (json.dumps({"owner": owner, "ts": ts}),))
            return True

    def release_lease(self, owner: str) -> None:
        with self._tx() as db:
            row = db.execute("SELECT value FROM meta WHERE key='lease'").fetchone()
            if row is not None and f'"{owner}"' in row["value"]:
                db.execute("DELETE FROM meta WHERE key='lease'")


class _Tx:
    """BEGIN IMMEDIATE ... COMMIT, so concurrent processes serialize their writes."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")


def _rule(row: Optional[sqlite3.Row]) -> Optional[dict]:
    if row is None:
        return None
    data: dict[str, Any] = dict(row)
    data["config"] = json.loads(data["config"])
    data["enabled"] = bool(data["enabled"])
    return data


def _event(row: sqlite3.Row) -> dict:
    data: dict[str, Any] = dict(row)
    data["payload"] = json.loads(data["payload"])
    return data
