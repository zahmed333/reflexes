"""Tests for the Desktop page's backend routes. Needs fastapi (present in Hermes's own Python)."""
import asyncio
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:
    import fastapi  # noqa: F401
    from fastapi import HTTPException
except ImportError:  # pragma: no cover
    fastapi = None


def run(coro):
    return asyncio.run(coro)


@unittest.skipIf(fastapi is None, "fastapi not installed in this interpreter")
class BackendTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        (self.tmp / "inbox").mkdir()
        sys.modules.pop("_reflexes_backend", None)
        spec = importlib.util.spec_from_file_location("reflexes_plugin_api_under_test", ROOT / "reflexes" / "dashboard" / "plugin_api.py")
        self.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.api)
        self.api._package().default_db_path = lambda: self.tmp / "data" / "reflexes.db"
        self.api._setup_problem = lambda: ""
        self.store = self.api._ops().store
        self.addCleanup(self.store.close)
        self.store.see_session("agent:main:local:dm:local-abc123", "desktop")

    def body(self, **kw):
        base = {"folder": str(self.tmp / "inbox"), "pattern": "*.pdf", "instruction": "File it",
                "session_key": "agent:main:local:dm:local-abc123"}
        base.update(kw)
        return base

    def test_state_add_and_actions(self):
        state = run(self.api.state())
        self.assertEqual((state["rules"], state["events"], state["detecting"]), ([], [], False))
        self.assertEqual(state["sessions"][0]["surface"], "desktop")

        added = run(self.api.add_rule(self.body()))["rule"]
        self.assertEqual((added["name"], added["pattern"], added["surface"], added["enabled"]),
                         ("inbox", "*.pdf", "desktop", True))

        run(self.api.rule_action("inbox", "pause"))
        self.assertFalse(run(self.api.state())["rules"][0]["enabled"])
        run(self.api.rule_action("inbox", "resume"))
        run(self.api.rule_action("inbox", "test"))
        events = run(self.api.state())["events"]
        self.assertEqual((events[0]["rule"], events[0]["status"]), ("inbox", "pending"))

        self.store.see_session("ses_other", "tui")
        run(self.api.rule_action("inbox", "bind", {"session_key": "ses_other"}))
        self.assertEqual(run(self.api.state())["rules"][0]["session_key"], "ses_other")

        run(self.api.rule_action("inbox", "remove"))
        self.assertEqual(run(self.api.state())["rules"], [])

    def test_bad_requests_are_400_with_a_reason(self):
        cases = [
            self.api.add_rule(self.body(session_key="made-up-route")),
            self.api.add_rule(self.body(folder=str(self.tmp / "missing"))),
            self.api.add_rule(self.body(instruction="")),
            self.api.rule_action("ghost", "pause"),
            self.api.rule_action("ghost", "explode"),
        ]
        for coro in cases:
            with self.assertRaises(HTTPException) as caught:
                run(coro)
            self.assertEqual(caught.exception.status_code, 400)
            self.assertTrue(caught.exception.detail)

    def test_drive_and_log_rules(self):
        drive = run(self.api.add_rule({"kind": "drive", "pattern": "KINGSTON*", "events": ["connected", "removed"],
                                       "instruction": "Import", "session_key": "agent:main:local:dm:local-abc123"}))["rule"]
        self.assertEqual((drive["kind"], drive["target"], drive["folder"], drive["file"]),
                         ("drive", "drives matching KINGSTON*", "", ""))
        self.assertEqual((drive["pattern"], drive["events"]), ("KINGSTON*", ["connected", "removed"]))
        logfile = self.tmp / "inbox" / "build.log"
        logfile.write_bytes(b"old\n")
        log = run(self.api.add_rule({"kind": "log", "file": str(logfile), "pattern": "/ERR(OR)?/",
                                     "instruction": "Look", "session_key": "agent:main:local:dm:local-abc123"}))["rule"]
        self.assertEqual((log["kind"], log["file"], log["folder"]), ("log", str(logfile), ""))
        self.assertEqual(log["target"], "build.log · matches /ERR(OR)?/")
        folder = run(self.api.add_rule(self.body()))["rule"]
        self.assertEqual((folder["kind"], folder["target"]), ("folder", f"{self.tmp / 'inbox'} · *.pdf"))
        rules = {r["name"]: r for r in run(self.api.state())["rules"]}
        self.assertEqual({r["kind"] for r in rules.values()}, {"folder", "drive", "log"})
        run(self.api.rule_action(drive["name"], "test"))
        run(self.api.rule_action(log["name"], "test"))
        events = run(self.api.state())["events"]
        self.assertEqual({e["status"] for e in events}, {"pending"})
        self.assertEqual(len(self.store.pending_events()), 2)
        run(self.api.rule_action(log["name"], "remove"))

    def test_bad_drive_and_log_requests_are_400(self):
        key = "agent:main:local:dm:local-abc123"
        cases = [
            self.api.add_rule({"kind": "usb", "instruction": "x", "session_key": key}),
            self.api.add_rule({"kind": "log", "file": str(self.tmp / "inbox" / "a.log"), "instruction": "x", "session_key": key}),
            self.api.add_rule({"kind": "log", "file": str(self.tmp / "inbox" / "a.log"), "pattern": "/(/",
                               "instruction": "x", "session_key": key}),
            self.api.add_rule({"kind": "log", "file": str(self.tmp / "gone" / "a.log"), "pattern": "x",
                               "instruction": "x", "session_key": key}),
            self.api.add_rule({"kind": "drive", "instruction": "", "session_key": key}),
        ]
        for coro in cases:
            with self.assertRaises(HTTPException) as caught:
                run(coro)
            self.assertEqual(caught.exception.status_code, 400)
            self.assertTrue(caught.exception.detail)


if __name__ == "__main__":
    unittest.main()
