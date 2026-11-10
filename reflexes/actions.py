"""What users and the model can do with reflexes: the shared operations behind the tools and the
``/reflex`` slash command."""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable, Optional

from . import sources
from .store import Store, slugify

KINDS = ("folder", "drive", "log")

HELP = """Reflexes: start a turn in this chat when something happens on this computer.

/reflex                                   list reflexes
/reflex add <folder> [pattern] :: <what to do>
                                          e.g. /reflex add ~/Downloads *.pdf :: File it under ~/Documents by year
/reflex add drive [label-pattern] :: <what to do>
                                          when a USB stick / SD card / external disk is connected,
                                          e.g. /reflex add drive KINGSTON* :: Copy the photos to ~/Pictures/import
/reflex add log <file> <text-or-/regex/> :: <what to do>
                                          when matching lines are appended to a log file,
                                          e.g. /reflex add log ~/logs/build.log ERROR :: Find the cause and tell me
                                          (quote paths that contain spaces)
/reflex pause|resume|remove <name>
/reflex test <name>                       send one fake firing to check delivery
/reflex here [name]                       deliver to this chat from now on (all reflexes, or one)
/reflex log [n]                           recent firings

Reflex turns appear live in Hermes Desktop, `hermes --tui` and messaging chats. The classic
`hermes chat` terminal runs them but does not display them (Hermes reports both terminals as the
same surface, so this cannot be detected for you)."""


def _ago(ts: Optional[float], now: Optional[float] = None) -> str:
    if not ts:
        return "never"
    delta = max(0, int((now or time.time()) - ts))
    for unit, secs in (("d", 86400), ("h", 3600), ("m", 60)):
        if delta >= secs:
            return f"{delta // secs}{unit} ago"
    return f"{delta}s ago"


def _drive_target(cfg: dict) -> str:
    pattern = ", ".join(sources.parse_globs(cfg.get("pattern", "*")))
    return "any drive" if pattern == "*" else f"drives matching {pattern}"


def _log_what(pattern: Any) -> str:
    try:
        return sources.compile_log_pattern(str(pattern or "")).describe()
    except ValueError:
        return f"matches {pattern}"


def _log_target(cfg: dict) -> str:
    file = str(cfg.get("file", ""))
    return f"{os.path.basename(file) or file} {_DOT} {_log_what(cfg.get('pattern'))}"


def rule_target(rule: dict) -> str:
    """One short human string for what the rule watches."""
    cfg, kind = rule["config"], rule["kind"]
    if kind == "drive":
        return _drive_target(cfg)
    if kind == "log":
        return _log_target(cfg)
    return f"{cfg.get('path', '')} {_DOT} {cfg.get('pattern', '*')}"


def request_target(args: dict) -> tuple[str, str]:
    """(kind, target) for a not yet validated reflex_add request, for the approval prompt."""
    kind = str(args.get("kind") or "folder").strip().lower()
    if kind == "drive":
        events = " or ".join(e for e in (args.get("events") or ["connected"]) if isinstance(e, str)) or "connected"
        return kind, f"{_drive_target({'pattern': args.get('pattern') or '*'})} ({events})"
    if kind == "log":
        file, pattern = str(args.get("file") or "?"), str(args.get("pattern") or "?")
        return kind, f"{file} for lines {_log_what(pattern)}"
    return "folder", f"{args.get('folder') or '?'} (files: {args.get('pattern') or '*'})"


def describe_rule(rule: dict) -> str:
    cfg, kind = rule["config"], rule["kind"]
    if kind == "drive":
        events = "/".join(cfg.get("events") or ["connected"])
        return f"drive {events}: {_drive_target(cfg)}"
    if kind == "log":
        return f"new lines in {cfg.get('file', '')} that {_log_what(cfg.get('pattern'))}"
    events = "/".join(cfg.get("events") or ["created"])
    where = cfg["path"] + (" (and subfolders)" if cfg.get("recursive") else "")
    return f"{events} files matching {cfg.get('pattern', '*')} in {where}"


def rule_view(rule: dict) -> dict[str, Any]:
    state = "armed" if rule["enabled"] else "paused"
    cfg = rule["config"]
    view: dict[str, Any] = {
        "name": rule["name"], "kind": rule["kind"], "target": rule_target(rule),
        "folder": cfg.get("path", "") if rule["kind"] == "folder" else "",
        "state": state, "trigger": describe_rule(rule), "instruction": rule["prompt"],
        "problem": rule["health"] or rule["paused_reason"] or "",
        "delivers_to": rule["surface"] or ("this terminal" if not rule["session_key"] else "its chat"),
        "fired": rule["fire_count"], "last_fired": _ago(rule["last_fired_at"]),
    }
    if rule["kind"] == "log":
        view["file"] = cfg.get("file", "")
    return view


_DOT = "·"
_ARMED_NOTE = {
    "folder": "Armed. Files already in the folder are ignored; it fires for changes from now on, in this chat, "
              "while Hermes is running.",
    "drive": "Armed. Drives already connected are ignored; it fires when a matching drive is connected from now on, "
             "in this chat, while Hermes is running.",
    "log": "Armed. Existing lines in the file are ignored; it fires when new matching lines are appended, "
           "in this chat, while Hermes is running.",
}


class Actions:
    def __init__(self, store: Store, *, session: Callable[[], tuple[str, str]], enqueue_test: Callable[[dict], int],
                 max_per_hour: Callable[[], int] = lambda: 30, setup_problem: Callable[[], str] = lambda: ""):
        self.store = store
        self._setup_problem = setup_problem  # -> what the user must do before anything can be delivered, or ''
        self._ambient_session = session  # -> (session_key, surface) of the calling chat
        self._explicit = threading.local()  # what a slash command was told about its chat
        self._enqueue_test = enqueue_test
        self._max_per_hour = max_per_hour

    def setup_problem(self) -> str:
        try:
            return self._setup_problem() or ""
        except Exception:
            return ""

    def _session(self) -> tuple[str, str]:
        explicit = getattr(self._explicit, "value", None)
        if explicit is not None:
            return explicit
        return self._ambient_session()

    # -- operations ------------------------------------------------------------------------------
    def add(self, *, instruction: str, folder: str = "", name: str = "", pattern: str = "*", events: Any = None,
            recursive: bool = False, session_key: Optional[str] = None, surface: Optional[str] = None,
            kind: str = "folder", file: str = "") -> dict:
        instruction = (instruction or "").strip()
        kind = (kind or "folder").strip().lower()
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)} (got {kind!r}).")
        if not instruction:
            raise ValueError("Say what to do when it fires (the instruction is empty).")
        if len(instruction) > 4000:
            raise ValueError("The instruction is too long (limit 4000 characters).")
        offset: Optional[int] = None
        if kind == "folder":
            path = sources.validate_folder(folder, recursive=bool(recursive))
            chosen = [e for e in (events or ["created"]) if e in sources.FILE_EVENTS] or ["created"]
            pats = sources.parse_patterns(pattern or "*")
            sources.scan(str(path), pats, bool(recursive))  # fail now, not silently later, if unreadable/too big
            label = path.name
            config = {"path": str(path), "pattern": ", ".join(pats), "events": chosen, "recursive": bool(recursive)}
        elif kind == "drive":
            globs = sources.parse_globs(pattern or "*")
            chosen = [e for e in (events or ["connected"]) if e in sources.DRIVE_EVENTS] or ["connected"]
            label = "drive-" + slugify(globs[0]) if globs != ["*"] else "drives"
            config = {"pattern": ", ".join(globs), "events": chosen}
        else:
            logfile = sources.validate_log_file(file)
            pattern = (pattern or "").strip()
            pattern = "" if pattern == "*" else pattern  # the generic default means "not given" here
            sources.compile_log_pattern(pattern)  # required: plain text or /regex/
            label = logfile.name
            config = {"file": str(logfile), "pattern": pattern}
            try:  # start at the end now, so lines written before the first poll are not lost
                offset = logfile.stat().st_size if logfile.exists() else 0
            except OSError:
                offset = None
        key, surf = self._session()
        if session_key is not None:
            key, surf = session_key, surface or surf
        self.store.see_session(key, surf)
        rule = self.store.add_rule(
            name=name or label, kind=kind, config=config,
            prompt=instruction, session_key=key, surface=surf, max_per_hour=int(self._max_per_hour()),
        )
        if offset is not None:
            self.store.save_log_offset(rule["id"], offset)
        return rule

    def _need(self, name: str) -> dict:
        if not (name or "").strip():
            raise ValueError("Which reflex? Give its name (see /reflex list).")
        rule = self.store.get_rule(name.strip())
        if rule is None:
            known = ", ".join(r["name"] for r in self.store.list_rules()) or "none armed yet"
            raise ValueError(f"No reflex named {name!r}. Known: {known}.")
        return rule

    def pause(self, name: str) -> dict:
        rule = self._need(name)
        self.store.set_enabled(rule["id"], False, "paused by you")
        return self.store.get_rule(rule["id"])  # type: ignore[return-value]

    def resume(self, name: str) -> dict:
        rule = self._need(name)
        self.store.set_enabled(rule["id"], True)
        return self.store.get_rule(rule["id"])  # type: ignore[return-value]

    def remove(self, name: str) -> dict:
        rule = self._need(name)
        self.store.remove_rule(rule["id"])
        return rule

    def test(self, name: str) -> dict:
        rule = self._need(name)
        self._enqueue_test(rule)
        return rule

    def rebind(self, name: str = "") -> list[dict]:
        key, surf = self._session()
        rules = [self._need(name)] if (name or "").strip() else self.store.list_rules()
        for rule in rules:
            self.store.bind_session(rule["id"], key, surf)
        return rules

    def log(self, limit: int = 10) -> list[dict]:
        out = []
        for ev in self.store.recent_events(max(1, min(int(limit or 10), 50))):
            kind = ev["payload"].get("type", "") if isinstance(ev.get("payload"), dict) else ""
            out.append({"reflex": ev["rule_name"], "kind": kind if kind in KINDS else "", "what": ev["summary"],
                        "when": _ago(ev["created_at"]),
                        "status": ev["status"], "note": ev["detail"]})
        return out

    # -- tool handlers (always return a JSON string, never raise) --------------------------------
    def tool_add(self, args: dict, **_kw: Any) -> str:
        try:
            rule = self.add(folder=args.get("folder") or "", instruction=args.get("instruction", ""),
                            name=args.get("name", ""), pattern=args.get("pattern") or "*",
                            events=args.get("events"), recursive=bool(args.get("recursive")),
                            kind=args.get("kind") or "folder", file=args.get("file") or "")
            view = rule_view(rule)
            view["note"] = _ARMED_NOTE[rule["kind"]]
            out: dict[str, Any] = {"ok": True, "reflex": view}
            if self.setup_problem():
                out["user_must_do_first"] = self.setup_problem()
            return json.dumps(out)
        except ValueError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        except sources.SourceError as exc:
            return json.dumps({"ok": False, "error": f"Cannot watch that: {exc}"})
        except Exception as exc:  # a tool handler must not raise
            return json.dumps({"ok": False, "error": f"reflex_add failed: {exc}"})

    def tool_manage(self, args: dict, **_kw: Any) -> str:
        action, name = str(args.get("action", "")).lower(), str(args.get("name", ""))
        try:
            if action == "list":
                out: dict[str, Any] = {"ok": True, "reflexes": [rule_view(r) for r in self.store.list_rules()]}
                if self.setup_problem():
                    out["user_must_do_first"] = self.setup_problem()
                return json.dumps(out)
            if action == "log":
                return json.dumps({"ok": True, "firings": self.log(args.get("limit") or 10)})
            if action in ("pause", "resume", "remove", "test"):
                rule = getattr(self, action)(name)
                done = {"pause": "paused", "resume": "armed again", "remove": "removed",
                        "test": "test firing queued; it arrives as a new turn in the reflex's chat"}[action]
                return json.dumps({"ok": True, "reflex": rule["name"], "result": done})
            return json.dumps({"ok": False, "error": "action must be list, pause, resume, remove, test or log"})
        except ValueError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"reflex_manage failed: {exc}"})

    # -- slash command ---------------------------------------------------------------------------
    def command(self, raw_args: str = "", **kw: Any) -> str:
        """``/reflex ...``. Hermes passes the calling chat as ``session_key`` / ``platform`` keywords."""
        if kw.get("session_key"):
            self._explicit.value = (str(kw["session_key"]), str(kw.get("platform") or ""))
        try:
            return self._command((raw_args or "").strip())
        finally:
            self._explicit.value = None

    def _command(self, text: str) -> str:
        out = self._dispatch(text)
        problem = self.setup_problem()
        return f"{out}\n\n⚠ {problem}" if problem and not text.startswith("help") else out

    def _dispatch(self, text: str) -> str:
        verb, _, rest = text.partition(" ")
        verb, rest = verb.lower(), rest.strip()
        try:
            if verb in ("", "list", "ls"):
                return self._render_list()
            if verb in ("help", "-h", "--help"):
                return HELP
            if verb == "add":
                return self._command_add(rest)
            if verb == "pause":
                return f"Paused '{self.pause(rest)['name']}'."
            if verb == "resume":
                return f"'{self.resume(rest)['name']}' is armed again (files that arrived while paused are ignored)."
            if verb in ("remove", "rm", "delete"):
                return f"Removed '{self.remove(rest)['name']}'."
            if verb == "test":
                return f"Queued a test firing for '{self.test(rest)['name']}'. It arrives here as a new turn in a few seconds."
            if verb == "here":
                rules = self.rebind(rest)
                return (f"{len(rules)} reflex(es) now deliver to this chat." if rules
                        else "No reflexes yet. Add one with /reflex add.")
            if verb == "log":
                return self._render_log(int(rest) if rest.isdigit() else 10)
            return f"Unknown subcommand {verb!r}.\n\n{HELP}"
        except (ValueError, sources.SourceError) as exc:
            return f"Could not do that: {exc}"

    def _command_add(self, rest: str) -> str:
        head, sep, instruction = rest.partition("::")
        head = head.strip()
        first, after = _take_token(head)
        verb = first.lower() if head[:1] not in ('"', "'") else ""
        if not sep or not instruction.strip():
            return _ADD_USAGE
        if verb == "drive":
            pattern = _unquote(after) or "*"
            rule = self.add(kind="drive", pattern=pattern, instruction=instruction)
            tail = "Drives already connected are ignored. A new one starts a turn in this chat while Hermes is running."
        elif verb == "log":
            file, pattern = _take_token(after)
            pattern = _unquote(pattern)
            if not file or not pattern:
                return _LOG_USAGE
            rule = self.add(kind="log", file=file, pattern=pattern, instruction=instruction)
            tail = "Existing lines are ignored. New matching lines start a turn in this chat while Hermes is running."
        else:
            folder, pattern = _split_folder_pattern(head)
            rule = self.add(folder=folder, pattern=pattern, instruction=instruction)
            tail = "Files already there are ignored. New ones start a turn in this chat while Hermes is running."
        return f"Armed '{rule['name']}': {describe_rule(rule)}.\n{tail}"

    def _render_list(self) -> str:
        rules = self.store.list_rules()
        if not rules:
            return "No reflexes yet.\n\n" + HELP
        lines = []
        for rule in rules:
            view = rule_view(rule)
            lines.append(f"{'●' if rule['enabled'] else '○'} {view['name']}  [{view['state']}]  {view['kind']}: {view['target']}")
            lines.append(f"    does: {view['instruction'][:120]}")
            lines.append(f"    fired {view['fired']}x, last {view['last_fired']}")
            if view["problem"]:
                lines.append(f"    problem: {view['problem']}")
        return "\n".join(lines)

    def _render_log(self, limit: int) -> str:
        rows = self.log(limit)
        if not rows:
            return "Nothing has fired yet."
        return "\n".join(f"{r['when']:>8}  {r['reflex']}{' (' + r['kind'] + ')' if r['kind'] else ''}: {r['what']}  [{r['status']}]"
                         + (f"  ({r['note']})" if r["note"] else "") for r in rows)


def _split_folder_pattern(head: str) -> tuple[str, str]:
    """'<folder> [pattern]' where the folder may be quoted or contain spaces."""
    if head[:1] in ('"', "'"):
        end = head.find(head[0], 1)
        if end > 0:
            return head[1:end], head[end + 1:].strip() or "*"
    expanded = os.path.expandvars(os.path.expanduser(head))
    if os.path.isdir(expanded):
        return head, "*"
    folder, _, pattern = head.rpartition(" ")
    return (folder, pattern) if folder else (head, "*")


_LOG_USAGE = (
    "Usage: /reflex add log <file> <text-or-/regex/> :: <what to do>\n"
    "Example: /reflex add log ~/logs/build.log ERROR :: Find the cause (quote paths that contain spaces)"
)
_ADD_USAGE = (
    "Usage:\n"
    "  /reflex add <folder> [pattern] :: <what to do>\n"
    "  /reflex add drive [label-pattern] :: <what to do>\n"
    "  /reflex add log <file> <text-or-/regex/> :: <what to do>\n"
    "Example: /reflex add ~/Downloads *.pdf :: Summarize it"
)


def _take_token(text: str) -> tuple[str, str]:
    """First whitespace-delimited word (or a quoted string) and the rest. Backslashes are kept as typed."""
    text = text.strip()
    if text[:1] in ('"', "'"):
        end = text.find(text[0], 1)
        if end > 0:
            return text[1:end], text[end + 1:].strip()
    head, _, rest = text.partition(" ")
    return head, rest.strip()


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        return text[1:-1].strip()
    return text
