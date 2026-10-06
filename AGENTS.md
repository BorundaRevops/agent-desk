# Agent Desk repository guidance

This repository is a generic, public starter kit. Never copy a user's live ledger, credentials, private messages, customer data, internal URLs, personal names, or organization-specific workflow into it.

Read `README.md` and the role skill relevant to the user's request. Configure real chat identities only from the current environment. A work request does not grant merge, deployment, publication, access expansion, or external-send authority. Keep queued delivery, agent acceptance, completed work, PR merge, deployment, and live verification separate.

The installed CLI is `agent-desk`. Its local SQLite ledger is the source of truth. The browser view is read-only. Inspect `truncated` flags on snapshots; do not treat a bounded projection as complete history. The dispatcher is disabled by default and must be bound to a real Codex task before enablement. Preserve wake budgets, pauses, pending batches, and uncertain queue attempts.

For code changes, use a disposable `DESK_DB`, run focused tests, and scan the full tracked tree for private identifiers and secrets before publishing.
