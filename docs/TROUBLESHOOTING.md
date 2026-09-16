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
