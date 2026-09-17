# Quick Start

## 1. Requirements

You need:

- Ubuntu/Linux
- Docker Engine
- Docker Compose v2
- Working host microphone and speakers through PipeWire or PulseAudio
- OpenAI API key
- Google Cloud OAuth client JSON, with the Google Calendar API and Gmail API
  enabled
- Optional: the Joplin desktop app, for notes

Check Docker:

```bash
docker --version
docker compose version
```

Check the host sound server socket:

```bash
ls -l /run/user/$(id -u)/pulse/native
```

## 2. Put the Google OAuth file in the project

Save it as:

```text
client_secret.json
```

at the project root.

The authorized redirect URI must be:

```text
http://localhost:8000/oauth2callback
```

See `docs/GOOGLE_OAUTH.md`.

## 3. Generate local secrets and environment settings

```bash
chmod +x setup.sh
./setup.sh
```

## 4. Add your OpenAI API key

Open:

```bash
nano .env
```

Replace:

```text
OPENAI_API_KEY=sk-replace_me_with_your_openai_api_key
```

with your API key.

`setup.sh` already restricts the file with `chmod 600 .env`.

## 5. Optional: connect Joplin

In the Joplin desktop app, open **Tools > Options > Web Clipper**, enable the
service, and copy the authorization token into `.env`:

```text
JOPLIN_TOKEN=your_token_here
```

Leave it empty to run Gary without notes. See
[Configuration](docs/CONFIGURATION.md#joplin).

## 6. If you use a VPN

NordVPN's firewall blocks Docker networks that are not allowlisted, so the
voice container cannot reach the backend. Allowlist the assistant's network:

```bash
nordvpn allowlist add subnet 172.30.99.0/24
```

For other VPNs, allow the same subnet as local traffic.

## 7. Build

```bash
docker compose build
```

The selected faster-whisper model and Piper voice are downloaded during the
voice image build, and CrewAI (for Gary's specialist team) during the backend
build. The first build can take 20 minutes or more.

## 8. Start

```bash
docker compose up -d
```

Check containers:

```bash
docker compose ps
```

`backend`, `voice`, and `joplin-proxy` should be running.

Follow logs:

```bash
docker compose logs -f
```

The voice logs should show:

```text
Connected to backend. Listening locally for wake word: Gary
```

## 9. Connect Google Calendar and Gmail

Open:

```text
http://localhost:8000
```

Sign in with Google once and approve the Calendar and Gmail permissions. The
page should then show **Gmail: connected**.

## 10. Try the assistant

Say:

```text
Gary, add a dentist appointment Friday at 2 PM.
```

The local Whisper container detects the wake word first. Only after activation
does it stream the buffered pre-roll and live microphone audio to the backend.

Or set up some work:

```text
Gary, I need to publish the Chief of Staff video by Friday. What should I work on first?
```

Or ask the team:

```text
Gary, have Susan research three ideas for the next experiment.
```

More examples are in `docs/USAGE.md` and `docs/TEAM.md`.

## 11. Know what runs on its own

- **Scheduled planning** runs on weekdays at 8:00, 12:30, and 17:30. Each run
  makes one OpenAI request, may put ready tasks on your calendar within working
  hours (9 to 5), writes **Gary › Daily Summaries** in Joplin, and speaks a
  short briefing. Set `PLANNING_TIMES=` in `.env` to turn it off.
- **Announcements** for new email (hourly), due follow-ups, overdue tasks, and
  scheduled work that passed unfinished (every five minutes), except during
  quiet hours (10 PM to 7 AM). Unfinished scheduled work during the day also
  triggers a replan.
- **Human limits**: planning keeps to 9 to 5 on weekdays and never schedules
  over lunch (`PROTECTED_TIMES`, default 12:00-13:00).
- **Approvals**: anything that needs your OK waits at
  `http://localhost:8000/approvals`, or answer Gary by voice.
- **Backups** of `data/gary.db` go to `data/backups` daily.
- **The team**: Susan, Dave, Linda, and Catherine only work when Gary (or you,
  through the CLI) gives them an assignment. Gary announces when their reports
  are ready; see them at `http://localhost:8000/team` and in their Joplin
  notebooks.
- **Catherine's card**: give her a debit card at `http://localhost:8000/finance`.
  Every purchase she requests waits for you on the approvals page, and nothing
  is charged yet.

## 12. Stop

```bash
docker compose down
```

Google refresh credentials remain encrypted in `data/token_store.enc`, and
projects, tasks, and approvals remain in `data/gary.db`.
