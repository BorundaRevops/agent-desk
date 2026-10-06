# Agent Desk

Agent Desk is a local coordination ledger for a person working with several AI agents. It keeps work ownership, requests, conversations, pull requests, and release evidence in one SQLite file. A small read-only web view shows what needs attention. An optional, event-driven Codex dispatcher can wake an **existing** dispatcher task when new work arrives.

This repository is a starter kit. It contains no hosted publisher, CRM connector, credentials, personal work history, or pre-enrolled agents. It does not create Codex or Claude conversations for you; setup binds the real conversations you choose.

## Start here

Requires Python 3.9 or newer. The optional dispatcher supports macOS and Linux and requires a Codex CLI with `codex queue` support.

```sh
git clone https://github.com/BorundaRevops/agent-desk.git
cd agent-desk
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
agent-desk init --name "You"
agent-desk doctor
agent-desk serve
```

Open `http://127.0.0.1:8766`. The view is local and read-only. The ledger lives at `~/.local/state/agent-desk/coordination.sqlite3` by default with owner-only file permissions. Set `AGENT_DESK_HOME` before initialization to choose another private state directory. Never commit the database or config.

To have Codex or Claude set up your team, point it at this checkout and say:

> Read `AGENTS.md` and `skills/desk-setup/SKILL.md`. Set up Agent Desk for my existing work, using real chat identities. Establish a Desk Manager and a separate Codex dispatcher task if the tools on this machine support it. Update my active global agent instructions so each future agent registers its own real conversation and names its lane. Preserve my existing instructions. Keep delivery disabled until you test the exact target.

That explicit request lets the setup agent create or bind the conversations. It must not guess a session ID or silently turn on a watcher. Your Chief of Staff can be a separate agent or one of your existing conversations; see [the CoS protocol](docs/chief-of-staff.md).

### Make enrollment stick

The repository's `AGENTS.md` and `CLAUDE.md` apply only while an agent is working in this checkout. During setup, have the setup agent locate and update the **active personal/global instruction file** for each agent tool you use (for example, Codex's global `AGENTS.md` or Claude's global `CLAUDE.md`). Preserve its existing instructions, add the absolute path to this checkout, and include this rule:

> For each new agent conversation doing work managed in Agent Desk, read `skills/desk-agent/SKILL.md` from my Agent Desk checkout. Register your **real** provider, conversation ID, and host in Desk before taking ownership. Give your lane a stable, human-readable name that says what you own; use the same name for the chat or task when the tool allows it. Reuse an existing matching lane, and bind only workstreams actually assigned to you. If Desk or the real conversation ID is unavailable, say so instead of inventing a lane or claiming that delivery is connected.

The setup agent should show you the exact global-file change and verify one real lane registration. Each agent registers its own conversation; a shared CoS or Desk Manager registration does not cover other agents.

## What the ledger records

| Record | Meaning |
| --- | --- |
| Lane and consumer | The real agent conversation and its current inbox connection. Registration alone does not prove delivery. |
| Project, arc, card | The larger objective, owner narrative, and one workstream's next step. |
| Message and request | Durable conversation and a specific human decision, with exact response state. |
| PR and promotion | Ownership, merge, and deployment evidence as separate facts. |

Agents use `agent-desk COMMAND --json '{...}'`. Run `agent-desk --help` and `agent-desk COMMAND --help` for the exact contracts. Commands are trusted **local assertions**; the caller must verify external facts before recording them. Use a distinct `DESK_DB` for experiments.

```sh
agent-desk lane-register --json '{"provider":"codex","session_id":"ACTUAL-TASK-UUID","host_id":"local","label":"Desk Manager"}'
agent-desk snapshot --json '{"limit":1000,"request_history_limit":1000}'
```

The snapshot reports `truncated` flags. When a flag is true, use a scoped or paged read instead of claiming complete coverage. A user reply, queued message, accepted lease, merge, deployment, and live verification are different states.

## Optional Codex dispatcher

The Desk Manager first creates or selects a **real, existing** Codex dispatcher task, then binds its UUID:

```sh
agent-desk configure-dispatcher --thread ACTUAL-TASK-UUID
agent-desk dispatch enable
agent-desk dispatch once
agent-desk dispatch watch
```

`enable` checks the queue command, starts from the current ledger position, and does **not** dispatch old history. Run `watch` under your own process supervisor if you want continuous delivery. It observes local changes without running a model; on a new actionable batch, it queues the dedicated dispatcher task once. The dispatcher follows [its skill](skills/desk-dispatcher/SKILL.md), routes only verified work to existing owner tasks, and acknowledges the batch. The default is four wake attempts per hour and 16 per day, with local quiet hours from 10 p.m. to 8 a.m. A timeout or failed queue attempt becomes uncertain and is never retried automatically. `agent-desk dispatch disable` stops new delivery without deleting work.

If `codex` is not on your shell's `PATH`, pass `--codex-path /absolute/path/to/codex` to `configure-dispatcher`.

This transport is optional and machine-specific. If `agent-desk doctor` says `codex_queue_available: false`, use Desk manually until your Codex installation supports a verified queue transport. Claude agents can use their own supported session inbox mechanism; this package does not impersonate or wake a closed Claude session.

## Skills and roles

- [Desk setup](skills/desk-setup/SKILL.md) binds real conversations and establishes the manager and dispatcher.
- [Desk Manager](skills/desk-manager/SKILL.md) audits ownership, requests, messages, and PR state.
- [Desk Dispatcher](skills/desk-dispatcher/SKILL.md) routes bounded event batches without taking over owner work.
- [Desk Agent](skills/desk-agent/SKILL.md) enrolls and handles its own inbox.
- [Desk recovery](skills/desk-recover/SKILL.md) checks the exact existing installation after a restart.
- [Chief of Staff protocol](docs/chief-of-staff.md) describes coordination and evidence boundaries.

Skills are ordinary Markdown in this repository. Ask your agent to read the relevant file from the checkout; installing them into a personal skill directory is optional.

## Development

```sh
python -m unittest discover -s tests -v
python -m compileall -q agent_desk
```

The web view binds only `127.0.0.1`, accepts no writes, and does not expose the SQLite database over the network. Avoid placing sensitive content in public issues or example fixtures. Review changes to dispatcher wake logic and request/PR state transitions with tests.

MIT licensed. Contributions that keep the core local, generic, and dependency-light are welcome.
