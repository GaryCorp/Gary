# Project File Manifest

## Root

### `README.md`

Project overview and documentation index.

### `QUICKSTART.md`

Shortest installation path.

### `compose.yaml`

Defines the backend and local Whisper containers.

### `.env.example`

Complete environment-variable template.

### `.gitignore`

Prevents local credentials and runtime data from being committed.

### `.dockerignore`

Reduces Docker build context and excludes secrets.

### `setup.sh`

Creates `.env`, random local secrets, host UID/GID, and audio GID.

### `Makefile`

Convenience commands.

### `CHANGELOG.md`

Summary of the final build.

### `client_secret.json`

Not included because this is your private Google credential file.

You must add it yourself.

## `backend/`

### `backend/Dockerfile`

Builds the FastAPI service.

### `backend/requirements.txt`

Backend Python dependencies.

### `backend/app/__init__.py`

Python package marker.

### `backend/app/main.py`

Contains:

- FastAPI routes;
- Google OAuth;
- encrypted token persistence;
- Calendar API read/create helpers;
- Realtime WebSocket bridge;
- constrained calendar read and create function tools.

## `voice/`

### `voice/Dockerfile`

Builds the CPU-only local Whisper audio service.

### `voice/requirements.txt`

Voice-container dependencies.

### `voice/voice_client.py`

Contains:

- microphone input;
- local wake transcription;
- wake-word detection;
- pre-roll;
- Realtime audio forwarding;
- assistant audio playback.

## `scripts/`

### `scripts/diagnose.sh`

Checks Docker, host audio, configuration, and container audio visibility.

### `scripts/check_python.sh`

Runs Python syntax checks on both application modules.

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

At runtime the backend may create:

```text
data/token_store.enc
```

That runtime credential file is intentionally not included in the archive.
