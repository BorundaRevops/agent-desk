import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import subprocess
import unittest
from unittest.mock import patch

from agent_desk import desk, dispatcher, setup


class DispatcherRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.db = self.base / "coordination.sqlite3"
        self.config_path = self.base / "config.json"
        self.calls = []
        def fake_run(args, **kwargs):
            self.calls.append(args)
            return SimpleNamespace(returncode=0)
        self.config = {"schema_version": 1, "human_lane": "unused", "dispatcher_thread": "dispatcher-task",
            "dispatcher_lane": "unused", "dispatch_enabled": False, "codex_path": "fake-codex",
            "poll_seconds": 1, "max_wakes_per_hour": 4, "max_wakes_per_day": 16,
            "quiet_start_hour": 22, "quiet_end_hour": 8}
        setup._write_json(self.config_path, self.config)
        self.patches = [patch.object(desk, "DEFAULT_DB", self.db),
            patch.object(dispatcher, "DEFAULT_DB", self.db),
            patch.object(dispatcher, "STATE_DIR", self.base),
            patch.object(dispatcher, "STATE", self.base / "dispatch-state.json"),
            patch.object(dispatcher, "LOCK", self.base / "dispatch.lock"),
            patch.object(dispatcher, "HEALTH", self.base / "dispatcher-status.json"),
            patch.object(setup, "CONFIG", self.config_path),
            patch.object(dispatcher, "CONFIG", self.config_path),
            patch.object(dispatcher, "_quiet", return_value=False),
            patch.object(dispatcher.subprocess, "run", side_effect=fake_run)]
        for p in self.patches:
            p.start(); self.addCleanup(p.stop)
        self.ledger = desk.Desk()
        self.human = self.ledger.execute("lane-register", {"provider": "human", "session_id": "owner",
            "host_id": "local", "label": "You"})["lane_id"]
        self.dispatch_lane = self.ledger.execute("lane-register", {"provider": "codex",
            "session_id": "dispatcher-task", "host_id": "local", "label": "Dispatcher"})["lane_id"]
        self.worker = self.ledger.execute("lane-register", {"provider": "codex",
            "session_id": "worker-task", "host_id": "local", "label": "Worker"})["lane_id"]
        self.ledger.execute("consumer-register", {"lane_id": self.worker,
            "mechanism": "native_inbox", "metadata": {}, "ttl_seconds": 900})
        self.config["dispatcher_lane"] = self.dispatch_lane
        setup._write_json(self.config_path, self.config)

    def test_one_event_one_batch_and_one_route(self):
        self.assertTrue(dispatcher.enable()["enabled"])
        self.ledger.execute("message-send", {"from_lane": self.human, "to_lane": self.worker,
            "kind": "work_request", "dedup_key": "first", "body": "Please check the report."})
        first = dispatcher.once()
        self.assertEqual(first["status"], "queued")
        self.assertEqual(first["events"], 1)
        self.assertEqual(dispatcher.once()["batch"], first["batch"])
        pending = dispatcher.status()["pending"]
        event = pending["events"][0]
        routed = dispatcher.route(event["key"], self.worker)
        self.assertEqual(routed["status"], "queued")
        self.assertTrue(dispatcher.route(event["key"], self.worker)["duplicate"])
        self.assertEqual(len(self.calls), 3)  # one help check and two queue attempts
        dispatcher.acknowledge(first["batch"], "Routed to owner")
        self.assertEqual(dispatcher.once()["events"], 0)

    def test_uncertain_queue_is_not_retried(self):
        dispatcher.enable()
        self.ledger.execute("message-send", {"from_lane": self.human, "to_lane": self.worker,
            "kind": "question", "dedup_key": "uncertain", "body": "Please inspect."})
        def timeout(args, **kwargs):
            self.calls.append(args)
            raise subprocess.TimeoutExpired(args, 20)
        with patch.object(dispatcher.subprocess, "run", side_effect=timeout):
            first = dispatcher.once()
            self.assertEqual(first["status"], "uncertain")
            self.assertEqual(dispatcher.once()["status"], "uncertain")
        self.assertEqual(len(self.calls), 2)  # help check, then one uncertain send
        with self.assertRaises(desk.DeskError):
            dispatcher.acknowledge(first["batch"], "No handling evidence")

    def test_pause_preserves_events_and_same_owner_routes_once(self):
        dispatcher.enable()
        for index in (1, 2):
            self.ledger.execute("message-send", {"from_lane": self.human, "to_lane": self.worker,
                "kind": "question", "dedup_key": "message-" + str(index), "body": "Question"})
        self.ledger.execute("pause", {"paused": True})
        self.assertEqual(dispatcher.once()["status"], "paused")
        self.assertEqual(len(self.calls), 1)
        self.ledger.execute("pause", {"paused": False})
        batch = dispatcher.once()
        self.assertEqual(batch["events"], 2)
        keys = [event["key"] for event in dispatcher.status()["pending"]["events"]]
        self.assertEqual(dispatcher.route(keys[0], self.worker)["status"], "queued")
        self.assertEqual(dispatcher.route(keys[1], self.worker)["status"], "coalesced")
        self.assertEqual(len(self.calls), 3)  # help, dispatcher, one worker


if __name__ == "__main__":
    unittest.main()
