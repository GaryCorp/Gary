# AI Calendar Assistant — Complete Docker Project

A privacy-oriented Ubuntu voice assistant that uses:

- **Local faster-whisper** for the wake word.
- The wake word **"Gary"**.
- A short **pre-roll audio buffer**, so the beginning of the command is not lost.
- **OpenAI Realtime** only after local wake-word activation.
- **FastAPI** as the local backend.
- **Google Calendar API** through constrained create and read tools.
- **Docker Compose** with separate `voice` and `backend` containers.

## Core privacy behavior

Before the wake word is detected:

    microphone
        ↓
    /dev/snd
        ↓
    voice container
        ↓
    local faster-whisper

No microphone audio is intentionally sent to OpenAI before activation.

After the wake word is detected:

    short pre-roll + live microphone audio
        ↓
    backend container
        ↓
    OpenAI Realtime
        ↓
    optional calendar read/create function call
        ↓
    Google Calendar

## Project layout

See [`docs/PROJECT_FILES.md`](docs/PROJECT_FILES.md) for the complete manifest.

## Start here

1. Read [`QUICKSTART.md`](QUICKSTART.md).
2. Put your Google OAuth file at `client_secret.json`.
3. Run `./setup.sh`.
4. Add your OpenAI API key to `.env`.
5. Build and start with Docker Compose.
6. Sign into Google once at `http://localhost:8000`.
7. Say: **"Gary, add a dentist appointment Friday at 2 PM."**

Or ask: **"Gary, what's on my calendar tomorrow?"** The assistant retrieves
matching events and reads a concise chronological summary aloud.

## Documentation

- [`QUICKSTART.md`](QUICKSTART.md)
- [`docs/SETUP.md`](docs/SETUP.md)
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md)
- [`docs/GOOGLE_OAUTH.md`](docs/GOOGLE_OAUTH.md)
- [`docs/USAGE.md`](docs/USAGE.md)
- [`docs/SECURITY.md`](docs/SECURITY.md)
- [`docs/PRIVACY.md`](docs/PRIVACY.md)
- [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md)
- [`docs/PROJECT_FILES.md`](docs/PROJECT_FILES.md)

## Important scope

This is a **single-user, local-machine application**. The backend is bound to
`127.0.0.1:8000` on the host. It is not intended to be exposed directly to the
public Internet.
