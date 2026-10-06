---
name: desk-manager
description: Audit Agent Desk ownership, inboxes, requests, and PR evidence; route exact corrections to existing owners.
---

# Manage Desk

Use `agent-desk snapshot --json '{"limit":1000,"request_history_limit":1000}'` and inspect every `truncated` flag. Read the current owner narratives, requests, inbox states, PR subscriptions, and dispatcher status. A snapshot is a projection; use scoped or paged reads when it is truncated.

Compare the records with current source evidence. Distinguish a PR being opened, merged, deployed, and live-verified. A user response is neither automatic verification nor permission for a larger step. Correct only a conclusively stale Desk-owned record with its exact owner/version/evidence. Route anything requiring the owner to the **existing** owner lane with a stable deduplication key. Do not take over that agent's implementation.

Establish the dedicated Codex dispatcher using `skills/desk-setup/SKILL.md` when the user asks for setup. During ordinary audits, preserve its disabled state, quiet hours, finite budgets, and outstanding batches. An expired or disconnected lane has queued work, not completed work. Do not mark it healthy from registration alone.

Give the human a concise decision when one is needed. Stay quiet when status is accurate and healthy. A Desk audit grants no merge, deployment, publication, live-data repair, external send, or budget extension.
