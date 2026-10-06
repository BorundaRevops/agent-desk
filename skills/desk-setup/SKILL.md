---
name: desk-setup
description: Set up a new local Agent Desk with real human and agent conversations, a Desk Manager, and an optional Codex dispatcher.
---

# Set up Agent Desk

Read the repository `README.md` and `docs/architecture.md`. Work only on the user's own installation. Do not import any sample or another person's live database.

1. Install the package in a virtual environment, run `agent-desk init`, then `agent-desk doctor`. Open the local view with `agent-desk serve` when useful. Report the actual state directory and whether the browser view loads.
2. Ask which existing conversations should own Chief of Staff and Desk Manager roles. If the user explicitly requested new tasks, create them through the supported app and obtain their **real** task IDs. Register each real lane with `lane-register`; reuse matching registrations. A label is not an identity.
3. Have each agent register its own inbox consumer only after its real delivery mechanism is running and tested. Registration, queueing, acceptance, and task completion are separate.
4. For Codex dispatch, create or select the dedicated existing Codex task the user requested. Run `agent-desk configure-dispatcher --thread ACTUAL_UUID`. Have that task read `skills/desk-dispatcher/SKILL.md`. Check `agent-desk doctor`. Keep dispatch disabled if `codex queue` is unavailable or the exact task cannot be verified. Otherwise enable it, test a bounded event, and supervise `agent-desk dispatch watch` under the user's preferred local process manager.
5. Ask the Chief of Staff to read `docs/chief-of-staff.md` and the owner agents to read `skills/desk-agent/SKILL.md`. Create workstreams and cards only for actual user-directed work. Show one real message round trip and explain the statuses shown in Desk.

Do not assume the setup request authorizes external messages, merges, deployment, publication, or access changes. Keep the local browser bound to loopback. Leave disabled or uncertain delivery intact for recovery instead of creating duplicate tasks or repeated wake attempts.
