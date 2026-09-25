# Gary — a local voice assistant and AI Chief of Staff (Docker)

Gary is a privacy-oriented voice assistant for Ubuntu that manages your Google
Calendar, Gmail and Joplin notes, and runs **GaryCorp**: a small company of AI
employees that plans your projects and tasks, researches, reviews, keeps the
books and files your engineering work. Everything runs in Docker on one
machine, and nothing risky happens without your approval.

## What it does

**Voice**

- **Local wake word.** faster-whisper listens for **"Gary"** on your machine,
  with a short pre-roll buffer so the start of the command is not lost.
- **Local speech.** Piper TTS speaks every reply.
- In between, the utterance is transcribed and answered by a text model with
  Gary's tools (`VOICE_MODE=transcribe`, the default), or handled by a single
  OpenAI Realtime session (`VOICE_MODE=realtime`).

**Your calendar, email and notes**

- Google Calendar: read, create and delete events.
- Gmail: read, search, reply and send, with spoken confirmation before
  anything is sent. Gary can also have a mailbox of his own.
- Joplin notes in a Gary notebook, through the desktop app's Web Clipper API.

**Chief of Staff**

- Projects, tasks, dependencies, follow-ups, commitments, approvals and an
  audit log in SQLite, backed up daily.
- Planning cycles on weekdays at 08:00, 12:30 and 17:30 that schedule work into
  free blocks, protect lunch and working hours, and replan when scheduled work
  passes unfinished. A daily summary is written to Joplin.
- A management loop every 15 minutes that costs nothing unless something
  changed: a new report, a missed block, a due follow-up, an expiring approval.
- **Gary as your manager**: a morning assignment and an evening check-in,
  spoken and emailed, a daily report by email, and a weekly review in Joplin.
  None of these call a model.
- An optional **weekly video schedule** that plans each batch of episodes as
  projects and tasks, with fixed shoot and publish slots on your calendar.
- **Pause from anywhere**: email `pause` or `resume` to Gary's mailbox.

**The team**

- Five CrewAI specialists, each with limited permissions, returning structured
  reports and keeping notes in their own Joplin notebook:
  Susan (Research & Strategy), Dave (Security), Linda (Operations),
  Catherine (CFO) and Lauren (Ethics).
- Susan searches the web and, with a key, Perplexity. She can also run a
  multi-round **product search** that keeps going until the ranking of ideas
  stops changing.
- Catherine holds an encrypted debit card. You approve each purchase yourself
  on the approvals page.
- Lauren reviews decisions with the bundled **EASE** ethical decision-making
  framework.
- Gary can propose **hiring** a colleague or a **reorganisation**, but only as
  an argument. Approving it files an engineering ticket for you to change the
  code.

**Engineering tickets**

Gary turns objectives into private GitHub issues assigned to you, on a private
Project board, and keeps them in sync with his own tasks. He cannot change
code, repository visibility or GitHub permissions.

**Guardrails**

- The model proposes and Python decides. Every side effect is an action under
  an approval policy: green runs, yellow waits for you at
  `http://localhost:8000/approvals`, red is refused.
- Every model call is recorded and priced. `MAX_DAILY_AI_SPEND_USD` is a hard
  daily ceiling that stops everything that calls a model, voice included,
  until midnight.

## Core privacy behavior

Before the wake word is detected:

    microphone
        ↓
    host PipeWire/PulseAudio socket
        ↓
    voice container
        ↓
    local faster-whisper

No microphone audio is intentionally sent anywhere before activation.

After the wake word is detected (default `transcribe` mode):

    pre-roll + that one utterance
        ↓
    backend container
        ↓
    OpenAI transcription model → text model with Gary's tools
        ↓
    Google Calendar / Gmail / Joplin / local SQLite
        ↓
    reply text spoken locally by Piper

Conversations are sent with `store: false`. Everything Gary says unprompted
is recorded locally and mirrored to a **Gary › Spoken** notebook in Joplin.
The planning cycles, specialists and Perplexity searches send their own,
documented context. See [`docs/PRIVACY.md`](docs/PRIVACY.md) for exactly what
is sent where.

## Start here

1. Read [`QUICKSTART.md`](QUICKSTART.md).
2. Put your Google OAuth file at `client_secret.json`.
3. Run `make setup`. It generates `.env` with fresh secrets.
4. Add your OpenAI API key to `.env`, plus optional extras: Joplin token,
   Perplexity key, GitHub settings.
5. If you use NordVPN or another VPN, allowlist `172.30.99.0/24`.
6. `make up`, then `make logs` to watch it start.
7. Sign into Google once at `http://localhost:8000`.
8. Say: **"Gary, add a dentist appointment Friday at 2 PM."**
9. Optionally run the tests: `make test`.

Or ask:

- **"Gary, what's on my calendar tomorrow?"**
- **"Gary, do I have any new emails?"**
- **"Gary, make a note: call the plumber tomorrow."**
- **"Gary, I need to publish the video by Friday. What should I work on first?"**
- **"Gary, I'm thinking about giving you browser automation. Have your team evaluate it."**
- **"Gary, find me a startup product to build."**
- **"Gary, what have we spent?"**

See [`docs/USAGE.md`](docs/USAGE.md) for everything Gary can do. Scheduled
planning is on by default; set `PLANNING_TIMES=` in `.env` to turn it off.

## Running it

```bash
make up | down | logs | status | rebuild
make test                      # the backend suite, inside the backend image
```

After changing backend code, rebuild the image:
`docker compose build backend && docker compose up -d backend`.

The operator tools run inside the backend container
(`docker compose exec backend ...`):

```bash
python -m app.ask "..."                    # talk to Gary in text
python -m app.dry_run [--act]              # one planning cycle, observe-only by default
python -m app.team_cli team|assign|review|show
python -m app.product_cli start|status|show|stop
python -m app.hiring_cli list|show|dismiss
python -m app.costs report                 # what the AI costs, and whether the ceiling is enforceable
```

Read-only status pages on `http://localhost:8000`: `/team`, `/approvals`,
`/finance`, `/events`, `/management/status`, `/engineering/status`,
`/production/status`, `/costs`.

## Services

`compose.yaml` runs:

- `backend`: FastAPI, the whole domain.
- `voice`: wake word, microphone and Piper.
- `joplin-proxy`: a bridge to the Joplin desktop app on the host.
- `ease-api`, `ease-worker` and `ease-redis`: the EASE service Lauren uses.

`compose.gpu.yaml` (`make up-gpu`) runs Whisper on an NVIDIA GPU.

## Documentation

- [`QUICKSTART.md`](QUICKSTART.md): the shortest installation path
- [`docs/SETUP.md`](docs/SETUP.md): the detailed installation guide
- [`docs/GOOGLE_OAUTH.md`](docs/GOOGLE_OAUTH.md): Google Cloud, Calendar and Gmail setup, both mailboxes
- [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md): every `.env` setting
- [`docs/USAGE.md`](docs/USAGE.md): what to say to Gary, and letting the company run itself
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): components, tools and data flow
- [`docs/TEAM.md`](docs/TEAM.md): the five specialists, permissions, the product search, hiring, reviews, the debit card, EASE, costs
- [`docs/ENGINEERING.md`](docs/ENGINEERING.md): private GitHub tickets, including setup, permissions, lifecycle and sync
- [`docs/SECURITY.md`](docs/SECURITY.md): security decisions and limitations
- [`docs/PRIVACY.md`](docs/PRIVACY.md): where audio, email and notes travel
- [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md): common failures and fixes, including restoring a backup
- [`docs/PROJECT_FILES.md`](docs/PROJECT_FILES.md): what each file does
- [`CHANGELOG.md`](CHANGELOG.md): version history

## Important scope

This is a **single-user, local-machine application**. The backend is bound to
`127.0.0.1:8000` on the host. It is not meant to be exposed directly to the
public Internet.
