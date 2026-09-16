# Gary — AI Calendar Assistant (Docker)

A privacy-oriented Ubuntu voice assistant named **Gary** that manages your
Google Calendar, Gmail, and Joplin notes. It uses:

- **Local faster-whisper** for the wake word **"Gary"**.
- A short **pre-roll audio buffer**, so the beginning of the command is not lost.
- **OpenAI Realtime** only after local wake-word activation.
- **Local Piper TTS** to speak Gary's replies.
- **FastAPI** as the local backend.
- **Google Calendar API** to read, create, and delete events.
- **Gmail API** to read, search, reply to, and send email, with spoken
  confirmation before anything is sent.
- **Joplin** notes in a Gary notebook, through the desktop app's Web Clipper API.
- **Docker Compose** with separate `voice`, `backend`, and `joplin-proxy`
  services.

## Core privacy behavior

Before the wake word is detected:

    microphone
        ↓
    host PipeWire/PulseAudio socket
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
    optional tool call
        ↓
    Google Calendar / Gmail / Joplin
        ↓
    reply text spoken locally by Piper

See [`docs/PRIVACY.md`](docs/PRIVACY.md) for exactly what is sent where.

## Project layout

See [`docs/PROJECT_FILES.md`](docs/PROJECT_FILES.md) for the complete manifest.

## Start here

1. Read [`QUICKSTART.md`](QUICKSTART.md).
2. Put your Google OAuth file at `client_secret.json`.
3. Run `./setup.sh`.
4. Add your OpenAI API key, and optionally your Joplin token, to `.env`.
5. If you use NordVPN or another VPN, allowlist `172.30.99.0/24`.
6. Build and start with Docker Compose.
7. Sign into Google once at `http://localhost:8000`.
8. Say: **"Gary, add a dentist appointment Friday at 2 PM."**

Or ask:

- **"Gary, what's on my calendar tomorrow?"**
- **"Gary, do I have any new emails?"**
- **"Gary, make a note: call the plumber tomorrow."**

See [`docs/USAGE.md`](docs/USAGE.md) for everything Gary can do.

## Documentation

- [`QUICKSTART.md`](QUICKSTART.md) — shortest installation path
- [`docs/SETUP.md`](docs/SETUP.md) — detailed installation guide
- [`docs/GOOGLE_OAUTH.md`](docs/GOOGLE_OAUTH.md) — Google Cloud, Calendar, and Gmail setup
- [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) — all `.env` settings
- [`docs/USAGE.md`](docs/USAGE.md) — what to say to Gary
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — components, tools, and data flow
- [`docs/SECURITY.md`](docs/SECURITY.md) — security decisions and limitations
- [`docs/PRIVACY.md`](docs/PRIVACY.md) — where audio, email, and notes travel
- [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) — common failures and fixes
- [`docs/PROJECT_FILES.md`](docs/PROJECT_FILES.md) — what each file does
- [`CHANGELOG.md`](CHANGELOG.md) — version history

## Important scope

This is a **single-user, local-machine application**. The backend is bound to
`127.0.0.1:8000` on the host. It is not intended to be exposed directly to the
public Internet.
