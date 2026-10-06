import tempfile
from pathlib import Path
import unittest

from agent_desk.desk import Desk, DeskError


class LedgerRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = Desk(Path(self.tmp.name) / "desk.sqlite3", clock=lambda: 10000.0)
        self.human = self.desk.execute("lane-register", {
            "provider": "human", "session_id": "owner", "host_id": "test", "label": "You"})["lane_id"]
        self.worker = self.desk.execute("lane-register", {
            "provider": "codex", "session_id": "worker", "host_id": "test", "label": "Builder"})["lane_id"]
        self.cos = self.desk.execute("lane-register", {
            "provider": "claude", "session_id": "cos", "host_id": "test", "label": "Chief of Staff", "role": "cos"})["lane_id"]
        self.consumer = self.desk.execute("consumer-register", {
            "lane_id": self.worker, "mechanism": "native_inbox", "metadata": {}, "ttl_seconds": 900})["consumer_id"]

    def test_message_delivery_is_separate_from_resolution(self):
        first = self.desk.execute("message-send", {"from_lane": self.human, "to_lane": self.worker,
            "kind": "question", "dedup_key": "first", "body": "What is the status?"})
        self.assertEqual(first["state"], "queued")
        self.assertTrue(self.desk.execute("message-send", {"from_lane": self.human, "to_lane": self.worker,
            "kind": "question", "dedup_key": "first", "body": "What is the status?"})["duplicate"])
        claim = self.desk.execute("inbox-claim", {"lane_id": self.worker, "consumer_id": self.consumer})
        self.assertEqual(claim["messages"][0]["message_id"], first["message_id"])
        authority = {"lane_id": self.worker, "consumer_id": self.consumer, "claim_id": claim["claim_id"]}
        self.desk.execute("message-accept", {"message_id": first["message_id"], **authority})
        reply = self.desk.execute("message-reply", {"message_id": first["message_id"],
            "body": "Still working.", "dedup_key": "answer", **authority})
        self.assertEqual(reply["parent_id"], first["message_id"])
        self.desk.execute("inbox-release", authority)
        snapshot = self.desk.execute("snapshot", {"limit": 1000})
        self.assertEqual(snapshot["counts"]["messages"], 2)
        self.assertFalse(snapshot["truncated"]["messages"])

    def test_merge_does_not_claim_promotion(self):
        owned = self.desk.execute("pr-own", {"repo": "example/app", "pr": 7,
            "owner_lane": self.worker, "title": "Improve reports"})
        merged = self.desk.execute("pr-merge", {"repo": "example/app", "pr": 7,
            "owner_lane": self.worker, "expected_version": owned["version"], "merge_commit": "abc123"})
        self.assertEqual(merged["status"], "merged")
        self.assertIsNone(merged["attestation"])
        with self.assertRaises(DeskError):
            self.desk.execute("pr-promote", {"repo": "example/app", "pr": 7,
                "cos_lane": self.cos, "expected_version": merged["version"],
                "attestation": {"release": "r1", "deployed_commit": "def456",
                    "included_commits": ["abc123", "def456"], "runtime_evidence": []}})
        promoted = self.desk.execute("pr-promote", {"repo": "example/app", "pr": 7,
            "cos_lane": self.cos, "expected_version": merged["version"],
            "attestation": {"release": "r1", "deployed_commit": "def456",
                "included_commits": ["abc123", "def456"], "runtime_evidence": ["verified test endpoint"]}})
        self.assertEqual(promoted["status"], "promoted")


if __name__ == "__main__":
    unittest.main()
