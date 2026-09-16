# Full Setup Guide

## A. Install Docker

Install Docker Engine and Docker Compose v2 using the Docker documentation for
your Ubuntu release.

Verify:

```bash
docker --version
docker compose version
```

## B. Verify host audio

The voice container uses the host's PipeWire/PulseAudio server through its
socket, not the sound devices directly. Check the socket exists:

```bash
ls -l /run/user/$(id -u)/pulse/native
```

If it is missing, install `pipewire-pulse` (or PulseAudio) on the host.

The `voice` container receives only this socket.

## C. Prepare Google OAuth

Follow `GOOGLE_OAUTH.md`. Enable both the Google Calendar API and the Gmail
API.

Place the downloaded client file here:

```text
./client_secret.json
```

## D. Generate project configuration

```bash
chmod +x setup.sh
./setup.sh
```

This creates `.env` from `.env.example` if missing and generates:

- `SESSION_SECRET`
- `TOKEN_ENCRYPTION_KEY`
- `VOICE_BRIDGE_TOKEN`
- `HOST_UID` and `HOST_GID`
- `AUDIO_GID` (no longer used)

It also creates `data/` and restricts `.env` and `data/` permissions.

## E. Add the OpenAI API key

Edit:

```bash
nano .env
```

Set:

```text
OPENAI_API_KEY=...
```

Do not commit `.env`.

## F. Optional: connect Joplin

Gary can create, list, and delete notes in a **Gary** notebook in the Joplin
desktop app.

1. Open Joplin and go to **Tools > Options > Web Clipper**.
2. Enable the Web Clipper service. It listens on port 41184.
3. Copy the authorization token into `.env`:

   ```text
   JOPLIN_TOKEN=...
   ```

The `joplin-proxy` service lets the backend reach Joplin, which only listens on
the host's `127.0.0.1`. The Gary notebook is created on first use if it does
not exist. Joplin must be open whenever you want Gary to use notes.

Leave `JOPLIN_TOKEN` empty to run without notes.

## G. VPN

With NordVPN connected, its firewall drops traffic to Docker networks that are
not allowlisted, and the voice container cannot reach the backend. Allowlist
the assistant's network once:

```bash
nordvpn allowlist add subnet 172.30.99.0/24
```

The subnet is fixed in `compose.yaml` (`ASSISTANT_SUBNET`), so the entry keeps
working when the network is recreated. For other VPNs, allow the same subnet as
local or split-tunnel traffic.

## H. Build

```bash
docker compose build
```

The selected Whisper model and Piper voice are pre-downloaded into the voice
image. For an NVIDIA GPU, see
[Configuration](CONFIGURATION.md#gpu-nvidia).

## I. Start

```bash
docker compose up -d
```

Check:

```bash
docker compose ps
```

`backend` should be healthy, and `voice` and `joplin-proxy` running.

## J. Google login

Open:

```text
http://localhost:8000
```

Complete OAuth. The page should show your Google account and
**Gmail: connected**.

## K. Watch the logs

```bash
docker compose logs -f voice joplin-proxy
```

A healthy startup should include:

```text
Whisper ready.
Piper ready (22050 Hz).
Connected to backend. Listening locally for wake word: Gary
[joplin-proxy] forwarding 172.30.99.1:41184 to Joplin at 127.0.0.1:41184
```

## L. Test

Say:

```text
Gary, add a meeting tomorrow at 3 PM.
```

```text
Gary, do I have any new emails?
```

```text
Gary, make a note: test note from Gary.
```

If required details are missing, the assistant can ask a follow-up.

## M. Restart

```bash
docker compose restart
```

After changing backend code or `.env`, rebuild and recreate instead:

```bash
docker compose up -d --build
```

## N. Stop

```bash
docker compose down
```
