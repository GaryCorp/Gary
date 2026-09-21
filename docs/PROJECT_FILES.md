# Project File Manifest

## Root

### `README.md`

Project overview and documentation index.

### `QUICKSTART.md`

Shortest installation path.

### `compose.yaml`

Defines the backend, local Whisper voice, and Joplin proxy containers.

### `.env.example`

Complete environment-variable template.

### `.gitignore`

Prevents local credentials and runtime data from being committed.

### `compose.gpu.yaml`

Optional override that runs local Whisper on an NVIDIA GPU (`make up-gpu`).

### `setup.sh`

Creates `.env`, random local secrets, host UID/GID, and audio GID, and
restricts `.env` and `data/` permissions.

### `Makefile`

Convenience commands, including `make test`.

### `CHANGELOG.md`

Version history.

### `MANIFEST.json`

File list with sizes and SHA-256 hashes.

### `main.py`

PyCharm sample script. Not used by the assistant.

### `client_secret.json`

Not included because this is your private Google credential file.

You must add it yourself.

## `backend/`

### `backend/Dockerfile`

Builds the FastAPI service. Dependencies are installed with `uv`, using a
BuildKit cache so rebuilds do not download CrewAI again.

### `backend/requirements.txt`

Backend Python dependencies, including `crewai==1.15.22` (pinned: the adapter
in `gary/agents/crew.py` targets that release).

### `backend/requirements-dev.txt`

Test dependencies (pytest), used by `scripts/test.sh` and `make test`.

### `backend/gary/`

Chief of Staff operations package:

- `container.py` builds the services (`build_gary`);
- `policy.py` holds the action risk policy;
- `timeutil.py` holds the timestamp rules;
- `backup.py` makes SQLite backups;
- `planner.py` makes the single OpenAI call for a scheduled planning run;
- `db/` has the connection, migrations (`db/migrations/NNN_name.sql`), and
  repositories with all SQL;
- `models/` has the Pydantic request models;
- `services/` has business logic: tasks, projects, follow-ups, commitments,
  planning and scoring, actions, approvals, briefs (`briefing.py`), working
  time and free blocks (`calendar_blocks.py`), and the planning cycle
  (`planning_cycle.py`);
- `tools/` has the function tools exposed to Gary.

### `backend/gary/agents/`

GaryCorp specialist team (see [Team](TEAM.md)):

- `roster.py` defines Gary, Susan, Dave, Linda, Catherine, and Lauren: identity,
  prompts, tools, limits (the permission authority);
- `models.py` has agent definitions and the Research, Security, Operations,
  Finance, and Ethics report models;
- `gateway.py` is the tool catalog and permission-checking gateway;
- `context.py` builds least-privilege context packages;
- `executor.py` is the boundary to the agent framework, and `crew.py` the
  CrewAI executor;
- `runner.py` runs assignments; `service.py` handles delegation, management
  reviews, and reading reports;
- `web.py` is the read-only web research service;
- `ease.py` is the client for the EASE service, which condenses its analysis for Lauren.

### `backend/gary/integrations/github/`

The private engineering-ticket integration (see [Engineering](ENGINEERING.md)):
`config.py` (settings and credential provider), `client.py` (REST + Projects v2
GraphQL), `privacy.py` (the private-only gate), `projects.py` (Status field and
board operations), `issues.py` (issue body and labels), `models.py`,
`exceptions.py`, and `setup.py` (the verification command).
`gary/services/engineering_service.py` owns the ticket lifecycle,
`gary/db/repositories/engineering.py` its tables, and
`gary/tools/engineering_tools.py` Gary's twelve tools.

### `backend/gary/finance/`

Catherine's debit card (see [Team](TEAM.md#catherines-debit-card)):

- `cards.py` validates card numbers, encrypts the details into the card vault,
  and adds, freezes, and removes the card;
- `purchases.py` has the spending limits, the purchase payload, and the
  `card_purchase` action handler.

`db/migrations/003_finance.sql` adds `payment_cards`, and
`db/repositories/finance.py` reads cards, committed spend, purchases, and
specialist token usage.

### `backend/app/team_cli.py`

Command-line tool to run assignments and reviews by hand inside the backend
container.

### `backend/tests/`

pytest suite using temporary databases and fakes for Google, Joplin, OpenAI, and
the agent framework: operations, planning, Chief of Staff behavior, tools, the
specialist team (`test_agents.py`), Catherine's card and purchases
(`test_finance.py`), the GitHub engineering tickets (`test_engineering.py`,
with the in-memory GitHub in `fake_github.py`), and the CrewAI adapter (`test_crew.py`,
including an opt-in live test with `GARY_LIVE_AGENT_TEST=1`).

### `backend/app/__init__.py`

Python package marker.

### `backend/app/main.py`

The composition root. Builds the shared objects (Gary's operations package,
the planning cycle, the team, the cost ledger and spend ceiling) and contains:

- the background loops: planning scheduler, management loop, spoken delivery,
  weekly review, GitHub sync, missed-block replanning, and the new email and
  operations announcements;
- the read-only status endpoints (`/health`, `/costs`, `/management/status`,
  `/engineering/status`);
- the voice WebSocket bridge and the tool call entry point both voice paths use.

### `backend/app/pages.py`

The web pages: `/`, the Google sign-in routes (your account and Gary's
mailbox), `/events`, `/approvals`, `/team` and `/finance`.

### `backend/app/calendar_actions.py`

The `schedule_task`, `move_calendar_event` and `send_external_email` action
handlers for the operations package.

### `backend/app/notebooks.py`

The Joplin notebooks used outside a conversation: Planning and Daily
Summaries, each specialist's own notebook, and Gary > Spoken.

### `backend/app/announcements.py`

The words Gary says when he speaks first: operations alerts and approvals.

### `backend/app/tool_dispatch.py`

Runs the voice tools that reach Google Calendar, Gmail and Joplin.

### `backend/app/realtime.py` and `backend/app/realtime_session.py`

`VOICE_MODE=realtime`: the response turn-taking rules (no I/O), and the
OpenAI Realtime connection itself.

### `backend/app/voice_turn.py`

`VOICE_MODE=transcribe`: one utterance transcribed, then answered by a text
model.

### `backend/app/system_summary.py`

The deployment described without secrets, for Dave's security reviews.

### `backend/app/local_time.py`

Timestamps in local time, for the calendar and for being spoken.

### `backend/app/config.py`

Every setting read from the environment (`.env`), and fixed limits.

### `backend/app/google_auth.py`

The encrypted Google token store, the OAuth flow, and which account (yours or
Gary's own mailbox) a call goes through.

### `backend/app/google_calendar.py`

Calendar read, create, and delete helpers, and the busy-time reader planning
uses.

### `backend/app/gmail.py`

Gmail tools, the new email check, and the unread summaries planning uses.

### `backend/app/joplin.py`

Joplin note and notebook tools.

### `backend/app/voice_tools.py`

The tool definitions given to Gary's voice model.

### `backend/app/instructions.py`

Gary's system prompt for a voice session.

## `voice/`

### `voice/Dockerfile`

Builds the local Whisper and Piper TTS audio service (CPU by default, CUDA
with `compose.gpu.yaml`).

### `voice/requirements.txt`

Voice-container dependencies.

### `voice/voice_client.py`

Contains:

- microphone input;
- local wake transcription;
- wake-word detection;
- pre-roll;
- Realtime audio forwarding;
- local Piper TTS playback of Gary's replies;
- new email announcements.

### `voice/run_whisper.py`

Runs the voice client directly on the host from the project virtualenv instead
of in Docker, connecting to the backend on `127.0.0.1:8000`.

## `joplin_proxy/`

### `joplin_proxy/joplin_proxy.py`

Forwards the assistant network's gateway address to the Joplin desktop app's
Web Clipper API on the host's `127.0.0.1:41184`. Runs in the `joplin-proxy`
service using the stock `python:3.12-slim` image.

## `scripts/`

### `scripts/diagnose.sh`

Checks Docker, host audio, configuration, and container audio visibility.

### `scripts/test.sh`

Runs the backend test suite in the backend Docker image. `make test` calls it.

### `scripts/check_python.sh`

Runs Python syntax checks on the backend, voice client, and Joplin proxy.

## `docs/`

### `docs/ARCHITECTURE.md`

System architecture and data flow.

### `docs/SETUP.md`

Detailed installation guide.

### `docs/GOOGLE_OAUTH.md`

Google Cloud and OAuth configuration.

### `docs/CONFIGURATION.md`

All `.env` variables.

### `docs/USAGE.md`

How to use the assistant.

### `docs/SECURITY.md`

Security decisions and limitations.

### `docs/PRIVACY.md`

Where audio and credentials travel.

### `docs/TROUBLESHOOTING.md`

Common failures and commands.

### `docs/TEAM.md`

The specialist team: roles, permissions, reviews, limits, and CLI.

### `docs/PROJECT_FILES.md`

This file.

## `data/`

### `data/.gitkeep`

Keeps the directory in source archives.

At runtime the backend creates:

```text
data/token_store.enc          encrypted Google credentials
data/gary.db                  Chief of Staff database (plus -wal and -shm files)
data/backups/gary-YYYY-MM-DD.db   daily database backups
```

These runtime files are git-ignored and intentionally not included in the
archive.
