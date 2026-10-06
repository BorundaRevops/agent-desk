"""Bounded event observer for an existing Codex dispatcher task.

Observation uses local SQLite. Only a configured, explicitly enabled Codex
queue transport can wake a task. Queue attempts are reserved before sending;
uncertain attempts are never retried automatically.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from .desk import DEFAULT_DB, STATE_DIR, Desk, DeskError
from .setup import CONFIG, _write_json, load_config

STATE = STATE_DIR / "dispatch-state.json"
LOCK = STATE_DIR / "dispatch.lock"
HEALTH = STATE_DIR / "dispatcher-status.json"
WATCHED_ACTIONS = {"pr-own", "pr-merge", "pr-close", "pr-promote", "request-respond", "incident-report"}


@contextmanager
def locked_state():
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with LOCK.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            try:
                state = json.loads(STATE.read_text())
            except FileNotFoundError:
                state = {"cursor_messages": 0, "cursor_audit": 0, "pending": None,
                         "routes": {}, "attempts": []}
            yield state
            _write_json(STATE, state)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _db():
    if not DEFAULT_DB.exists():
        raise DeskError("Desk ledger does not exist; run `agent-desk init`")
    connection = sqlite3.connect(DEFAULT_DB.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _highwater():
    with _db() as con:
        return (con.execute("SELECT coalesce(max(rowid),0) FROM messages").fetchone()[0],
                con.execute("SELECT coalesce(max(rowid),0) FROM audit").fetchone()[0])


def _events(state):
    events = []
    with _db() as con:
        messages = con.execute("""SELECT m.rowid AS event_rowid,m.message_id,m.to_lane,l.provider,m.expires_at
            FROM messages m JOIN lanes l ON l.lane_id=m.to_lane
            WHERE m.rowid>? AND m.state='queued' ORDER BY m.rowid LIMIT 100""",
            (state["cursor_messages"],)).fetchall()
        audits = con.execute("SELECT rowid AS event_rowid,action,detail FROM audit WHERE rowid>? ORDER BY rowid LIMIT 100",
                             (state["cursor_audit"],)).fetchall()
        for m in messages:
            if m["provider"] == "codex" and m["expires_at"] > time.time():
                events.append({"key": "message:" + m["message_id"], "kind": "message",
                               "lane_id": m["to_lane"], "message_id": m["message_id"]})
        for row in audits:
            if row["action"] not in WATCHED_ACTIONS:
                continue
            try:
                detail = json.loads(row["detail"])
            except (ValueError, TypeError):
                continue
            lane = detail.get("owner_lane")
            if row["action"] == "request-respond":
                request = con.execute("SELECT owner_lane FROM requests WHERE request_id=?",
                                      (detail.get("request_id"),)).fetchone()
                lane = request["owner_lane"] if request else None
            elif row["action"].startswith("pr-"):
                pr = con.execute("SELECT owner_lane FROM prs WHERE repo=? AND pr=?",
                                 (detail.get("repo"), str(detail.get("pr")))).fetchone()
                lane = pr["owner_lane"] if pr else lane
            if lane:
                events.append({"key": "audit:" + str(row["event_rowid"]), "kind": row["action"],
                               "lane_id": lane,
                               "repo": detail.get("repo"), "pr": detail.get("pr")})
        newest_message = messages[-1]["event_rowid"] if messages else state["cursor_messages"]
        newest_audit = audits[-1]["event_rowid"] if audits else state["cursor_audit"]
    return events, newest_message, newest_audit


def _quiet(config):
    hour = datetime.now().astimezone().hour
    start = int(config.get("quiet_start_hour", 22))
    end = int(config.get("quiet_end_hour", 8))
    if start == end:
        return False
    return hour >= start or hour < end if start > end else start <= hour < end


def _budget(state, config):
    now = time.time()
    state["attempts"] = [t for t in state["attempts"] if t > now - 86400]
    if _quiet(config):
        raise DeskError("quiet hours are active")
    if sum(t > now - 3600 for t in state["attempts"]) >= config["max_wakes_per_hour"]:
        raise DeskError("hourly wake budget exhausted")
    if len(state["attempts"]) >= config["max_wakes_per_day"]:
        raise DeskError("daily wake budget exhausted")
    state["attempts"].append(now)


def _queue(thread, message, config):
    try:
        result = subprocess.run([config["codex_path"], "queue", "--thread", thread,
                                 "--message", message], capture_output=True, text=True, timeout=20)
        return result.returncode == 0, "queued" if result.returncode == 0 else "uncertain"
    except (OSError, subprocess.TimeoutExpired):
        return False, "uncertain"


def _health(status, pending=0):
    _write_json(HEALTH, {"status": status, "delivery": "event-queue",
                         "pending_events": pending, "checked_at": time.time()})


def enable():
    config = load_config()
    if not config.get("dispatcher_thread") or not config.get("dispatcher_lane"):
        raise DeskError("configure a real dispatcher task before enabling delivery")
    try:
        result = subprocess.run([config["codex_path"], "queue", "--help"],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeskError("Codex queue transport could not be verified") from exc
    if result.returncode:
        raise DeskError("Codex queue transport is unavailable")
    with locked_state() as state:
        if state["pending"]:
            raise DeskError("inspect the outstanding batch before enabling")
        state["cursor_messages"], state["cursor_audit"] = _highwater()
        config["dispatch_enabled"] = True
        _write_json(CONFIG, config)
    _health("watching")
    return {"enabled": True, "existing_events": "baselined; inspect existing Desk work manually"}


def disable():
    config = load_config()
    config["dispatch_enabled"] = False
    _write_json(CONFIG, config)
    _health("paused")
    return {"enabled": False, "pending_batch_preserved": True}


def once():
    config = load_config()
    if not config.get("dispatch_enabled"):
        _health("paused")
        return {"status": "paused"}
    with locked_state() as state:
        if state["pending"]:
            pending = state["pending"]
            _health(pending["status"], len(pending["events"]))
            return {"status": pending["status"], "batch": pending["id"],
                    "events": len(pending["events"])}
        events, last_message, last_audit = _events(state)
        if not events:
            state["cursor_messages"] = last_message
            state["cursor_audit"] = last_audit
            _health("watching")
            return {"status": "watching", "events": 0}
        if Desk().execute("snapshot", {"limit": 1})["paused"]:
            _health("paused", len(events))
            return {"status": "paused", "events": len(events)}
        try:
            _budget(state, config)
        except DeskError as exc:
            _health("deferred", len(events))
            return {"status": "deferred", "reason": str(exc), "events": len(events)}
        batch = "batch-" + str(int(time.time() * 1000))
        state["cursor_messages"] = last_message
        state["cursor_audit"] = last_audit
        state["pending"] = {"id": batch, "events": events, "status": "reserved",
                            "created_at": time.time()}
        # Persist the reservation before the external queue attempt. No retry
        # follows an ambiguous result, including after a process crash.
        _write_json(STATE, state)
        prompt = ("Agent Desk batch " + batch + ". Inspect with `agent-desk dispatch status`; "
                  "handle within existing user authority, route only actual work, then acknowledge the batch.")
        ok, status = _queue(config["dispatcher_thread"], prompt, config)
        state["pending"]["status"] = status
        _health(status, len(events))
        return {"status": status, "batch": batch, "events": len(events), "queue_confirmed": ok}


def status():
    config = load_config()
    with locked_state() as state:
        return {"enabled": config["dispatch_enabled"], "pending": state["pending"],
                "routes": state["routes"], "wake_attempts_24h": len(state["attempts"])}


def route(key, lane_id):
    config = load_config()
    with locked_state() as state:
        pending = state["pending"]
        if not pending or pending["status"] != "queued":
            raise DeskError("no confirmed dispatcher batch is awaiting routing")
        event = next((e for e in pending["events"] if e["key"] == key), None)
        if not event or event["lane_id"] != lane_id:
            raise DeskError("event does not belong to this lane in the current batch")
        if key in state["routes"]:
            return {"duplicate": True, **state["routes"][key]}
        previous = next((receipt for other_key, receipt in state["routes"].items()
                         if other_key != key and any(e["key"] == other_key for e in pending["events"])
                         and receipt["lane_id"] == lane_id), None)
        if previous:
            state["routes"][key] = {"lane_id": lane_id, "status": "coalesced",
                                    "with_status": previous["status"]}
            return {"event": key, "lane_id": lane_id, "status": "coalesced",
                    "with_status": previous["status"], "queue_confirmed": False}
        snapshot = Desk().execute("snapshot", {"lane_id": lane_id, "limit": 1})
        if snapshot["paused"]:
            raise DeskError("Desk is paused; preserve the event without waking a worker")
        lane = snapshot["lanes"][0] if snapshot["lanes"] else None
        if not lane or lane["provider"] != "codex" or lane_id == config["dispatcher_lane"]:
            raise DeskError("route target must be an existing worker Codex lane")
        consumer = lane.get("consumer") or {}
        metadata = consumer.get("metadata") or {}
        if (not lane["connected"] or metadata.get("retired") or metadata.get("paused")
                or metadata.get("enabled") is False):
            raise DeskError("worker inbox is disconnected or paused; leave the event for manual follow-up")
        if event["kind"] == "message":
            with _db() as con:
                current = con.execute("SELECT state,expires_at FROM messages WHERE message_id=?",
                                      (event["message_id"],)).fetchone()
            if not current or current["state"] not in ("queued", "delivered") or current["expires_at"] <= time.time():
                raise DeskError("message is no longer awaiting this worker")
        _budget(state, config)
        receipt = {"lane_id": lane_id, "status": "reserved", "attempted_at": time.time()}
        state["routes"][key] = receipt
        _write_json(STATE, state)
        prompt = ("Agent Desk event " + key + ". Check your scoped Desk inbox and current evidence. "
                  "Accept and resolve only your own work within existing user authority.")
        ok, outcome = _queue(lane["session_id"], prompt, config)
        receipt["status"] = outcome
        _health(pending["status"], len(pending["events"]))
        return {"event": key, "lane_id": lane_id, "status": outcome, "queue_confirmed": ok}


def acknowledge(batch, note):
    if not note.strip():
        raise DeskError("record a factual disposition before acknowledging")
    with locked_state() as state:
        pending = state["pending"]
        if not pending or pending["id"] != batch:
            raise DeskError("batch is not outstanding")
        if pending["status"] != "queued":
            raise DeskError("queue delivery is uncertain; inspect the actual task before clearing")
        state["pending"] = None
        state["last_ack"] = {"batch": batch, "note": note[:500], "at": time.time()}
        _health("watching")
        return state["last_ack"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("enable", "disable", "once", "watch", "status"):
        sub.add_parser(name)
    route_parser = sub.add_parser("route")
    route_parser.add_argument("--event", required=True)
    route_parser.add_argument("--lane", required=True)
    ack_parser = sub.add_parser("ack")
    ack_parser.add_argument("--batch", required=True)
    ack_parser.add_argument("--note", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "enable":
            result = enable()
        elif args.command == "disable":
            result = disable()
        elif args.command == "once":
            result = once()
        elif args.command == "status":
            result = status()
        elif args.command == "route":
            result = route(args.event, args.lane)
        elif args.command == "ack":
            result = acknowledge(args.batch, args.note)
        else:
            try:
                while True:
                    once()
                    time.sleep(load_config().get("poll_seconds", 15))
            except KeyboardInterrupt:
                return 0
        print(json.dumps(result, sort_keys=True))
        return 0
    except (DeskError, OSError, subprocess.TimeoutExpired, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
