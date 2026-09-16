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

```bash
ls -l /dev/snd
```

Typical systems show entries such as:

```text
controlC0
pcmC0D0c
pcmC0D0p
```

The `voice` container receives only `/dev/snd`.

## C. Prepare Google OAuth

Follow `GOOGLE_OAUTH.md`.

Place the downloaded client file here:

```text
./client_secret.json
```

## D. Generate project configuration

```bash
chmod +x setup.sh
./setup.sh
```

This creates `.env` if missing and generates:

- `SESSION_SECRET`
- `TOKEN_ENCRYPTION_KEY`
- `VOICE_BRIDGE_TOKEN`
- host UID/GID
- audio GID

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

## F. Build

```bash
docker compose build
```

The selected Whisper model is pre-downloaded into the voice image.

## G. Start

```bash
docker compose up -d
```

Check:

```bash
docker compose ps
```

## H. Google login

Open:

```text
http://localhost:8000
```

Complete OAuth.

## I. Watch the voice logs

```bash
docker compose logs -f voice
```

A healthy startup should include:

```text
Whisper ready.
Connected to backend. Listening locally for wake word: Gary
```

## J. Test

Say:

```text
Gary, add a meeting tomorrow at 3 PM.
```

If required details are missing, the assistant can ask a follow-up.

## K. Restart

```bash
docker compose restart
```

## L. Stop

```bash
docker compose down
```
