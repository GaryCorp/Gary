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

Builds the FastAPI service.

### `backend/requirements.txt`

Backend Python dependencies.

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

### `backend/tests/`

pytest suite for the operations package, using temporary databases.

### `backend/app/__init__.py`

Python package marker.

### `backend/app/main.py`

Contains:

- FastAPI routes;
- Google OAuth;
- encrypted token persistence;
- Calendar API read/create helpers;
- Realtime WebSocket bridge;
- constrained calendar read, create, and delete function tools;
- Gmail tools;
- Joplin note and notebook tools;
- Google Calendar and Gmail action handlers for the operations package;
- the approvals web page;
- the planning scheduler, Joplin planning notebook, and busy-calendar reader;
- the hourly new email check.

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
