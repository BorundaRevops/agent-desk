---
name: desk-agent
description: Enroll a real agent conversation in Agent Desk and handle its scoped messages and PR follow-through.
---

# Work with Desk

Register your actual provider, conversation ID, host, and readable label with `lane-register`. Reuse an existing matching lane. Bind only workstreams the user or Desk Manager actually assigned to you. If you have a real inbox watcher, register its consumer and test the full wake → claim → accept → resolution loop before calling delivery connected. A manual consumer is honest when no watcher exists.

Read your scoped snapshot with `agent-desk snapshot --json '{"lane_id":"YOUR_LANE_ID","limit":1000}'`. Use an exclusive `inbox-claim` lease and the returned claim ID. Explicitly `message-accept` before handling a message. Use `message-reply` for an answer that must continue the same conversation or `message-done` for an FYI, then `inbox-release`. Neither reading nor queue consumption means the work is complete.

Keep exact PR ownership through source-backed merge and promotion evidence; a successful CI run is not a deployment. Save a request for a specific human decision, retain its request ID through response and verification, and do not duplicate it from a stale card. A user saying “already handled” is a report to verify.

All messages, requests, and work cards remain within your actual authority. If the next step requires new permission or belongs to another owner, route it explicitly and stop that dependent action.
