# Troubleshooting

## Run the diagnostic script first

```bash
chmod +x scripts/diagnose.sh
./scripts/diagnose.sh
```

## Voice container cannot access the microphone

The voice container talks to the host's PipeWire/PulseAudio server through
its socket, not to `/dev/snd` directly. Check the socket exists:

```bash
ls -l /run/user/$(id -u)/pulse/native
```

If it is missing, install `pipewire-pulse` (or PulseAudio) on the host.
`HOST_UID` in `.env` must match your user ID.

Restart:

```bash
docker compose up -d --force-recreate voice
```

## Wrong microphone or speaker

List PortAudio devices:

```bash
docker compose run --rm voice \
  python -c "import sounddevice as sd; print(sd.query_devices())"
```

Put the appropriate index in `.env`:

```text
AUDIO_DEVICE=3
```

Then recreate the voice service.

## Whisper is too slow

Use:

```text
WHISPER_MODEL=tiny
```

Increase:

```text
WAKE_CHECK_INTERVAL_SECONDS=1.5
```

or:

```text
WAKE_CHECK_INTERVAL_SECONDS=2.0
```

This reduces how often local Whisper runs.

Rebuild if you change the Whisper model:

```bash
docker compose build --no-cache voice
docker compose up -d voice
```

## "Your session hit the maximum duration of 60 minutes"

Older versions held one OpenAI Realtime connection open for the life of the
voice service, so this error appeared roughly hourly and could interrupt a
conversation. The backend now connects per activation and closes the session
when the assistant sleeps. Rebuild if you still see it:

```bash
docker compose up -d --build
```

A renewal mid-conversation is logged as:

```text
[OpenAI session renewed]
```

## Gary never announces new email

Voice logs show `[new email] ...` for each announcement, and
`[New email check skipped: ...]` with the reason when a check fails, such as
Gmail access not being granted. Checks also skip quiet hours
(`EMAIL_CHECK_QUIET_HOURS`, default 10 PM to 7 AM) and only run while the voice
service is connected. The first check happens one interval after the voice
service connects.

## Voice logs repeatedly say reconnecting

Check backend:

```bash
docker compose logs backend
```

Check health:

```bash
curl http://127.0.0.1:8000/health
```

Expected:

```json
{"ok":true}
```

If the voice logs say `timed out during opening handshake` and the health check
also times out, a VPN is probably blocking Docker networking. See the next
section.

## Gary goes quiet partway through setting up work

Voice logs showing `OpenAI rate limit reached; retrying in ...` mean your
OpenAI account hit its Realtime tokens-per-minute limit. Each model response
carries Gary's prompt and tools (about 11,500 tokens), so a limit of 40,000 per
minute can be reached during multi-step planning. Gary retries twice after the
suggested wait; if the logs say `not retrying again`, ask again in a minute.
Higher OpenAI usage tiers raise the limit
(https://platform.openai.com/account/rate-limits).

## NordVPN or another VPN is connected

NordVPN's firewall drops traffic to local subnets that are not allowlisted,
including Docker networks. The voice container then cannot reach the backend:

```text
[voice] timed out during opening handshake; reconnecting...
```

and `curl http://127.0.0.1:8000/health` times out on the host, even though
the backend logs show it is healthy.

Allowlist the assistant's network (`ASSISTANT_SUBNET`, default
`172.30.99.0/24`) and restart the voice service:

```bash
nordvpn allowlist add subnet 172.30.99.0/24
docker compose restart voice
```

Check with:

```bash
nordvpn settings
```

The subnet should be listed under **Allowlisted subnets**. The allowlist entry
persists across VPN reconnects and reboots. For other VPNs, allow the same
subnet as local or split-tunnel traffic.

## Gary can't make notes

Gary tells you the reason. Check:

- The Joplin desktop app is open, and **Tools > Options > Web Clipper** shows
  the service as enabled on port 41184.
- `JOPLIN_TOKEN` in `.env` matches the token shown there. Restart the backend
  after changing it: `docker compose up -d backend`.
- The proxy is running:

  ```bash
  docker compose logs joplin-proxy
  ```

  Expected: `[joplin-proxy] forwarding 172.30.99.1:41184 to Joplin at
  127.0.0.1:41184`. If it keeps saying `cannot listen`, run
  `docker compose down` and `docker compose up -d`.

Test the whole path from the backend:

```bash
docker compose exec backend python -c "import asyncio; from app.main import list_joplin_notebooks as f; print(asyncio.run(f()))"
```

## Gary says an action is waiting for approval

Open `http://localhost:8000/approvals`, or say "Gary, what needs my approval?".
Approvals expire after 72 hours; ask Gary to propose the action again.

If an approved action shows as failed, the page lists the error, for example
Gmail access not granted or a calendar event that no longer exists.

## Operations database

Run the backend tests:

```bash
make test
```

If `make` is not installed (`make: command not found`), run the script it
calls, or install make with `sudo apt install make`:

```bash
./scripts/test.sh
```

The database and migrations are checked at backend startup; a failing
migration is rolled back and logged in `docker compose logs backend`. To
restore a backup:

```bash
docker compose stop backend
cp data/backups/gary-YYYY-MM-DD.db data/gary.db
rm -f data/gary.db-wal data/gary.db-shm
docker compose start backend
```

## Scheduled planning did not run or did nothing

- Runs happen at `PLANNING_TIMES` on `PLANNING_WEEKDAYS` only, and a run missed
  by more than 90 minutes (for example, the backend was stopped) is skipped
  until the next time.
- Each type runs once a day, even if it failed. Check the result:

  ```bash
  docker compose exec backend python -c "import sqlite3; c = sqlite3.connect('/data/gary.db'); [print(r) for r in c.execute('SELECT planning_type, status, started_at, error_message FROM planning_runs ORDER BY started_at DESC LIMIT 5')]"
  ```

- `failed` with an OpenAI error: check `OPENAI_API_KEY` and that
  `PLANNING_MODEL` is available to your key and is not a Realtime model (see
  [Choosing models](CONFIGURATION.md#choosing-models)).
- Completed but nothing scheduled: the plan in `plan_json` lists each rejected
  proposal with its reason (for example, outside working hours), and
  `calendar_error` if Google Calendar could not be read.
- No daily summary note: Joplin must be open with the Web Clipper enabled.
- No spoken briefing: the voice service must be connected, it is not quiet
  hours, and the model may return no briefing when nothing needs attention.

## The backend build takes a long time

The first backend build downloads CrewAI and its dependencies (the backend
image is about 1.3 GB), which can take 20 minutes or more on a slow connection.
Later builds reuse the download cache and only reinstall when
`backend/requirements.txt` changes. If a build seems stuck with no network
activity for several minutes, cancel it and run `docker compose build backend`
again.

## A specialist assignment failed

Check the error and run details:

```bash
docker compose exec backend python -m app.team_cli team
docker compose exec backend python -m app.team_cli show <assignment_id>
```

- `did not finish within N seconds`: the run hit `MAX_AGENT_EXECUTION_SECONDS`.
- `report failed validation`: the model's report was malformed twice; try again
  or use a more capable `GARY_EMPLOYEE_MODEL`.
- `Interrupted by an application restart`: the backend restarted mid-run;
  delegate again.
- `already has N assignments in progress`: wait, or raise
  `MAX_ACTIVE_AGENT_ASSIGNMENTS`.
- Rate limit errors from OpenAI: specialists share your account's limits with
  Gary's voice and planning; reduce `MAX_CONCURRENT_AGENT_RUNS` to 1.

## Google login loops

Confirm the Google OAuth redirect URI exactly matches:

```text
http://localhost:8000/oauth2callback
```

Also verify:

```text
client_secret.json
```

exists at the project root.

## Google token store cannot be decrypted

If you changed:

```text
TOKEN_ENCRYPTION_KEY
```

the existing encrypted token file no longer matches.

Reset local credentials:

```bash
docker compose down
rm -f data/token_store.enc
docker compose up -d
```

Then sign into Google again.

## OpenAI authentication failure

Check that `.env` contains:

```text
OPENAI_API_KEY=...
```

Recreate backend after changing environment variables:

```bash
docker compose up -d --force-recreate backend
```

If voice fails right after changing `OPENAI_REALTIME_MODEL`, check the backend
logs for a model error: the voice model must be a Realtime model
(`gpt-realtime-*`). General models such as `gpt-5.6-terra` only work as
`PLANNING_MODEL` (see [Choosing models](CONFIGURATION.md#choosing-models)).

## Compose validation

Run:

```bash
docker compose config
```

## Python syntax

Run:

```bash
chmod +x scripts/check_python.sh
./scripts/check_python.sh
```

## UFW

Do not add inbound port 8000 unless you have deliberately changed the local-only
deployment model.

The normal host binding is only:

```text
127.0.0.1:8000
```
