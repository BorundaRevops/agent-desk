"""Create a private local Desk and bind an existing Codex dispatcher task."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

from .desk import Desk, DeskError, DEFAULT_DB, STATE_DIR
from . import library

CONFIG = STATE_DIR / "config.json"


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def load_config():
    try:
        value = json.loads(CONFIG.read_text())
    except FileNotFoundError:
        raise DeskError("run `agent-desk init` first")
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise DeskError("unsupported or invalid config.json")
    return value


def init(name, host_id, library_root=None):
    if CONFIG.exists():
        value = load_config()
        chosen = library.ensure_root(library_root or value.get("library_root") or library.default_root())
        if value.get("library_root") != str(chosen):
            value["library_root"] = str(chosen)
            _write_json(CONFIG, value)
        print(json.dumps({"config": str(CONFIG), "db": str(DEFAULT_DB), "human_lane": value["human_lane"],
                          "library_root": str(chosen), "already_initialized": True}))
        return 0
    chosen = library.ensure_root(library_root or library.default_root())
    human = Desk().execute("lane-register", {
        "provider": "human", "session_id": "owner", "host_id": host_id, "label": name
    })
    value = {
        "schema_version": 1,
        "human_lane": human["lane_id"],
        "library_root": str(chosen),
        "dispatcher_lane": None,
        "dispatcher_thread": None,
        "dispatch_enabled": False,
        "codex_path": shutil.which("codex") or "codex",
        "poll_seconds": 15,
        "max_wakes_per_hour": 4,
        "max_wakes_per_day": 16,
        "quiet_start_hour": 22,
        "quiet_end_hour": 8,
    }
    _write_json(CONFIG, value)
    print(json.dumps({"config": str(CONFIG), "db": str(DEFAULT_DB), "human_lane": human["lane_id"],
                      "library_root": str(chosen), "already_initialized": False}))
    return 0


def configure_dispatcher(thread, label, codex_path=None):
    config = load_config()
    try:
        uuid.UUID(thread)
    except ValueError:
        raise DeskError("dispatcher thread must be the UUID of an existing Codex task")
    if config.get("dispatcher_thread") not in (None, thread):
        raise DeskError("a different dispatcher is configured; inspect existing work before replacing it")
    resolved = None
    if codex_path:
        resolved = shutil.which(codex_path) if not Path(codex_path).is_absolute() else codex_path
        if not resolved or not os.access(resolved, os.X_OK):
            raise DeskError("codex_path must name an executable Codex CLI")
    lane = Desk().execute("lane-register", {
        "provider": "codex", "session_id": thread,
        "host_id": "local", "label": label,
    })
    config["dispatcher_thread"] = thread
    config["dispatcher_lane"] = lane["lane_id"]
    if resolved:
        config["codex_path"] = resolved
    _write_json(CONFIG, config)
    print(json.dumps({"dispatcher_lane": lane["lane_id"], "dispatcher_thread": thread,
                      "dispatch_enabled": config["dispatch_enabled"]}))
    return 0


def doctor():
    config = load_config()
    snapshot = Desk().execute("snapshot", {"limit": 1000, "request_history_limit": 1000})
    codex = config.get("codex_path", "codex")
    executable = shutil.which(codex) if not Path(codex).is_absolute() else (codex if os.access(codex, os.X_OK) else None)
    queue_supported = False
    if executable:
        try:
            result = subprocess.run([executable, "queue", "--help"], capture_output=True, text=True, timeout=5)
            queue_supported = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    result = {
        "db_exists": DEFAULT_DB.exists(),
        "config": str(CONFIG),
        "library_root": str(library.configured_root()),
        "library_documents": len(library.list_documents()["files"]),
        "human_registered": any(l["lane_id"] == config["human_lane"] for l in snapshot["lanes"]),
        "dispatcher_configured": bool(config.get("dispatcher_thread") and config.get("dispatcher_lane")),
        "dispatch_enabled": bool(config.get("dispatch_enabled")),
        "codex_queue_available": queue_supported,
        "lanes": snapshot["counts"]["lanes"],
        "truncated": snapshot["truncated"],
    }
    print(json.dumps(result, sort_keys=True))
    return 0 if result["human_registered"] else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    first = sub.add_parser("init", help="create a local ledger and human lane")
    first.add_argument("--name", default="You", help="display name for the human lane")
    first.add_argument("--host-id", default="local", help="stable name for this installation")
    first.add_argument("--library-root", help="local HTML/Markdown folder (default: Desktop/Agent Desk/Library when Desktop exists)")
    bind = sub.add_parser("configure-dispatcher", help="bind a real, existing Codex task")
    bind.add_argument("--thread", required=True)
    bind.add_argument("--label", default="Desk Dispatcher")
    bind.add_argument("--codex-path", help="Codex CLI path when it is not on PATH")
    sub.add_parser("doctor", help="check local configuration without changing it")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            return init(args.name, args.host_id, args.library_root)
        if args.command == "configure-dispatcher":
            return configure_dispatcher(args.thread, args.label, args.codex_path)
        return doctor()
    except (DeskError, OSError, KeyError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
