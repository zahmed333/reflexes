"""Reflexes: start a Hermes turn when something happens on this computer.

A reflex is a standing rule ("when a PDF lands in ~/Downloads, file it") bound to the chat that
armed it. A background loop detects the event and injects it into that chat as a new turn through
``ctx.inject_message``; the agent then acts with its normal tools and normal approvals.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import schemas
from .actions import Actions, request_target, rule_target
from .engine import Engine, test_payload
from .store import Store, default_db_path

logger = logging.getLogger(__name__)

PLUGIN_ID = "reflexes"
NEEDS_PERMISSION = (
    "Hermes has not allowed this plugin to start turns yet, so reflexes are detected but not delivered. "
    "Allow it once with: hermes config set plugins.entries.reflexes.allow_gateway_injection true"
)
# Argument names under which Hermes's file tools carry the path they write.
_PATH_ARGS = ("path", "file_path", "filepath", "target", "destination", "dest", "output_path", "new_path")


class Runtime:
    """Everything one Hermes process needs, built lazily so a broken data dir never blocks loading."""

    def __init__(self, ctx: Any):
        self.ctx = ctx
        self._lock = threading.Lock()
        self._store: Optional[Store] = None
        self._engine: Optional[Engine] = None
        self._actions: Optional[Actions] = None
        self._armed_cache: tuple[float, bool] = (0.0, False)

    # -- settings (plugins.entries.reflexes.settings.*) ------------------------------------------
    def setting(self, key: str, default: Any) -> Any:
        try:
            value = self.ctx.get_config(key, default)
        except Exception:
            return default
        return default if value is None else value

    # -- lazy parts ------------------------------------------------------------------------------
    @property
    def store(self) -> Store:
        with self._lock:
            if self._store is None:
                self._store = Store(default_db_path())
            return self._store

    @property
    def engine(self) -> Engine:
        store = self.store
        with self._lock:
            if self._engine is None:
                self._engine = Engine(
                    store, self.inject, notify=self.notify, blocked=self.blocked,
                    poll_interval=_number(self.setting("poll_interval", 1.0), 1.0),
                    event_ttl=_number(self.setting("event_ttl_seconds", 900), 900.0),
                )
            return self._engine

    @property
    def actions(self) -> Actions:
        store, engine = self.store, self.engine
        with self._lock:
            if self._actions is None:
                self._actions = Actions(
                    store, session=self.session, enqueue_test=engine.enqueue_test, setup_problem=self.blocked,
                    max_per_hour=lambda: int(_number(self.setting("max_fires_per_hour", 30), 30)),
                )
            return self._actions

    # -- host bridges ----------------------------------------------------------------------------
    def session(self) -> tuple[str, str]:
        """(route key, surface) of the chat whose turn or command is running, ('', '') outside one."""
        key = surface = ""
        try:
            from gateway.session_context import get_session_env

            key = get_session_env("HERMES_SESSION_KEY", "") or ""
        except Exception:
            pass
        try:
            surface = self.ctx.session_surface() or ""
        except Exception:
            pass
        return key, surface

    def inject(self, message: str, session_key: str) -> bool:
        if session_key:
            return bool(self.ctx.inject_message(message, session_key=session_key))
        return bool(self.ctx.inject_message(message))  # classic CLI: the terminal chat itself

    def blocked(self) -> str:
        """Hermes lets a plugin start turns only after the user grants it, per plugin, in config.yaml.
        Without the grant every delivery is refused, so say so instead of retrying in silence."""
        try:
            from hermes_cli.config import load_config

            entries = ((load_config() or {}).get("plugins") or {}).get("entries") or {}
            if (entries.get(PLUGIN_ID) or {}).get("allow_gateway_injection") is True:
                return ""
        except Exception:
            return ""  # cannot tell: let the delivery attempt speak for itself
        return NEEDS_PERMISSION

    def notify(self, name: str, payload: dict) -> None:
        """Tell the Desktop half (when one is connected) that something happened."""
        from hermes_cli.plugin_events import broadcast_plugin_event

        broadcast_plugin_event(PLUGIN_ID, name, payload)

    def any_armed(self) -> bool:
        checked_at, armed = self._armed_cache
        now = time.time()
        if now - checked_at > 10:
            armed = self.store.count_rules() > 0
            self._armed_cache = (now, armed)
        return armed


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def register(ctx: Any) -> None:
    rt = Runtime(ctx)

    # Tools resolve the runtime lazily: registration itself touches no files and starts no I/O.
    ctx.register_tool(name="reflex_add", toolset="reflexes", schema=schemas.REFLEX_ADD,
                      handler=lambda args, **kw: rt.actions.tool_add(args, **kw),
                      description="Arm a rule that starts a turn when files change in a folder, a drive is connected or a log gets matching lines", emoji="⚡")
    ctx.register_tool(name="reflex_manage", toolset="reflexes", schema=schemas.REFLEX_MANAGE,
                      handler=lambda args, **kw: rt.actions.tool_manage(args, **kw),
                      description="List, pause, resume, remove or test reflexes", emoji="⚡")

    def reflex_command(raw_args: str = "", **kw: Any) -> str:
        try:
            return rt.actions.command(raw_args, **kw)
        except Exception as exc:
            logger.warning("reflexes: /reflex failed", exc_info=True)
            return f"Reflexes hit an error: {exc}"

    ctx.register_command("reflex", reflex_command, description="Start a turn here when files change, a drive is connected or a log line matches",
                         args_hint="[add|list|pause|resume|remove|test|here|log] ...")

    def pre_tool_call(tool_name: str = "", args: Optional[dict] = None, **_kw: Any) -> Optional[dict]:
        """A reflex the *model* arms keeps acting after this turn ends, so a person confirms it."""
        if tool_name != "reflex_add" or not rt.setting("approve_new_reflexes", True):
            return None
        args = args or {}
        kind, target = request_target(args)
        return {
            "action": "approve",
            "message": (f"Arm a {kind} reflex on {target}? "
                        f"Each time it fires, Hermes will start a turn in this chat with: "
                        f"{str(args.get('instruction', ''))[:300]}"),
        }

    def post_tool_call(tool_name: str = "", args: Optional[dict] = None, **_kw: Any) -> None:
        """Remember files the agent wrote, so a reflex does not fire on the agent's own output."""
        try:
            if not isinstance(args, dict) or not rt.any_armed():
                return
            base = ""
            paths = []
            for key in _PATH_ARGS:
                value = args.get(key)
                if not isinstance(value, str) or not value or len(value) > 1024:
                    continue
                path = Path(os.path.expanduser(value))
                if not path.is_absolute():
                    base = base or (ctx.session_cwd() or "")
                    if not base:
                        continue
                    path = Path(base) / path
                paths.append(os.path.normcase(os.path.abspath(path)))
            if paths:
                rt.store.touch(paths)
        except Exception:
            logger.debug("reflexes: post_tool_call bookkeeping failed", exc_info=True)

    def pre_llm_call(**_kw: Any) -> None:
        """A turn is running here: make sure the loop is up and let it deliver to this chat."""
        try:
            if ctx.current_cron_execution() is not None:
                return
            engine = rt.engine
            engine.start()
            key, surface = rt.session()
            engine.observe_session(key)
            rt.store.see_session(key, surface)  # lets the Desktop page offer this chat as a target
        except Exception:
            logger.debug("reflexes: pre_llm_call failed", exc_info=True)

    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_hook("pre_llm_call", pre_llm_call)

    skills_dir = Path(__file__).parent / "skills"
    if skills_dir.is_dir():
        for child in sorted(skills_dir.iterdir()):
            if child.is_dir() and (child / "SKILL.md").exists():
                ctx.register_skill(child.name, child / "SKILL.md")

    def start_engine() -> None:
        try:
            rt.engine.start()
        except Exception:
            logger.warning("reflexes: could not start (is the plugin data directory writable?)", exc_info=True)

    def stop_engine() -> None:
        if rt._engine is not None:
            rt._engine.stop()

    # Start off the registration path: a long-lived process (the gateway) must detect events even
    # when nobody has chatted since it started, while a one-shot command exits before any work.
    starter = threading.Timer(2.0, start_engine)
    starter.daemon = True
    starter.start()
    ctx.on_unload(lambda: (starter.cancel(), stop_engine()))
