"""Backend for the Reflexes Desktop page, mounted by Hermes at /api/plugins/reflexes/.

It reads and writes the same SQLite store as the agent half, so the page, the /reflex command and
the model's tools always agree. It never detects or delivers anything itself; the engine in the
agent half does that.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException

router = APIRouter()

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_PKG = "_reflexes_backend"


def _package() -> Any:
    """Hermes imports this file on its own, outside the plugin package, so load the package that
    sits one folder up under a private name to reach its store and operations."""
    module = sys.modules.get(_PKG)
    if module is None:
        spec = importlib.util.spec_from_file_location(
            _PKG, _PLUGIN_ROOT / "__init__.py", submodule_search_locations=[str(_PLUGIN_ROOT)])
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load the Reflexes plugin package")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_PKG] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(_PKG, None)
            raise
    return module


_actions: Any = None


def _ops() -> Any:
    global _actions
    if _actions is None:
        pkg = _package()
        store = pkg.Store(pkg.default_db_path())
        _actions = pkg.Actions(
            store, session=lambda: ("", ""), setup_problem=_setup_problem,
            enqueue_test=lambda rule: store.add_event(rule, "test firing", pkg.test_payload(rule)),
        )
    return _actions


def _setup_problem() -> str:
    pkg = _package()
    try:
        from hermes_cli.config import load_config

        entries = ((load_config() or {}).get("plugins") or {}).get("entries") or {}
        if (entries.get(pkg.PLUGIN_ID) or {}).get("allow_gateway_injection") is True:
            return ""
    except Exception:
        return ""
    return pkg.NEEDS_PERMISSION


def _fail(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _rule_json(rule: dict) -> dict:
    cfg, kind = rule["config"], rule["kind"]
    default_events = ["connected"] if kind == "drive" else ["created"]
    return {
        "id": rule["id"], "name": rule["name"], "enabled": rule["enabled"],
        "kind": kind, "target": _package().rule_target(rule),
        "folder": cfg.get("path", "") if kind == "folder" else "", "file": cfg.get("file", ""),
        "pattern": cfg.get("pattern", "" if kind == "log" else "*"), "events": cfg.get("events") or default_events,
        "recursive": bool(cfg.get("recursive")), "instruction": rule["prompt"],
        "problem": rule["health"] or rule["paused_reason"] or "",
        "session_key": rule["session_key"], "surface": rule["surface"],
        "fire_count": rule["fire_count"], "last_fired_at": rule["last_fired_at"],
    }


@router.get("/state")
async def state() -> dict:
    ops = _ops()
    store = ops.store
    lease_age = store.lease_age()
    return {
        "now": time.time(),
        "problem": ops.setup_problem(),
        "detecting": lease_age is not None and lease_age < 15,
        "rules": [_rule_json(r) for r in store.list_rules()],
        "events": [
            {"id": e["id"], "rule": e["rule_name"], "summary": e["summary"], "status": e["status"],
             "detail": e["detail"], "created_at": e["created_at"]}
            for e in store.recent_events(25)
        ],
        "sessions": store.recent_sessions(10),
    }


@router.post("/rules")
async def add_rule(body: dict) -> dict:
    session_key = str(body.get("session_key") or "")
    known = {s["key"]: s for s in _ops().store.recent_sessions(30)}
    if session_key not in known:  # only chats Hermes itself reported; the page cannot invent a route
        raise HTTPException(status_code=400, detail="Pick a chat to deliver to. Send any message in a chat first so it shows up here.")
    try:
        rule = _ops().add(
            kind=str(body.get("kind") or "folder"), file=str(body.get("file") or ""),
            folder=str(body.get("folder") or ""), instruction=str(body.get("instruction") or ""),
            name=str(body.get("name") or ""), pattern=str(body.get("pattern") or "*"),
            events=body.get("events"), recursive=bool(body.get("recursive")),
            session_key=session_key, surface=known[session_key].get("surface") or "",
        )
    except ValueError as exc:
        raise _fail(exc)
    except Exception as exc:  # SourceError and anything unexpected: say why, never a bare 500
        raise _fail(exc)
    return {"ok": True, "rule": _rule_json(rule)}


@router.post("/rules/{name}/{action}")
async def rule_action(name: str, action: str, body: Optional[dict] = None) -> dict:
    ops = _ops()
    try:
        if action in ("pause", "resume", "remove", "test"):
            getattr(ops, action)(name)
        elif action == "bind":
            session_key = str((body or {}).get("session_key") or "")
            known = {s["key"]: s for s in ops.store.recent_sessions(30)}
            if session_key not in known:
                raise ValueError("Unknown chat.")
            rule = ops._need(name)
            ops.store.bind_session(rule["id"], session_key, known[session_key].get("surface") or "")
        else:
            raise ValueError(f"Unknown action {action!r}.")
    except ValueError as exc:
        raise _fail(exc)
    return {"ok": True}
