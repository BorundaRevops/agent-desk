from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from agent_desk import desk, setup


class SetupRoundTrip(unittest.TestCase):
    def test_idempotent_init_and_real_dispatcher_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.object(desk, "DEFAULT_DB", base / "ledger.sqlite3"), \
                 patch.object(setup, "DEFAULT_DB", base / "ledger.sqlite3"), \
                 patch.object(setup, "CONFIG", base / "config.json"):
                self.assertEqual(setup.init("You", "local", base / "Library"), 0)
                self.assertEqual(setup.init("Another label", "local", base / "Library"), 0)
                thread = str(uuid.uuid4())
                self.assertEqual(setup.configure_dispatcher(thread, "Dispatcher"), 0)
                self.assertEqual(setup.configure_dispatcher(thread, "Dispatcher"), 0)
                self.assertEqual(setup.load_config()["dispatcher_thread"], thread)
                with self.assertRaises(desk.DeskError):
                    setup.configure_dispatcher(str(uuid.uuid4()), "Replacement")
                self.assertEqual(desk.Desk().execute("snapshot")["counts"]["lanes"], 2)


if __name__ == "__main__":
    unittest.main()
