# Quick Start

## 1. Requirements

You need:

- Ubuntu/Linux
- Docker Engine
- Docker Compose v2
- Working host microphone/speakers
- OpenAI API key
- Google Cloud OAuth client JSON
- Google Calendar API enabled

Check Docker:

```bash
docker --version
docker compose version
```

Check audio:

```bash
ls -l /dev/snd
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
OPENAI_API_KEY=replace_me
```

with your API key.

Then protect the file:

```bash
chmod 600 .env
```

## 5. Build

```bash
docker compose build
```

The selected faster-whisper model is downloaded during the voice image build.

## 6. Start

```bash
docker compose up -d
```

Check containers:

```bash
docker compose ps
```

Follow logs:

```bash
docker compose logs -f
```

## 7. Connect Google Calendar

Open:

```text
http://localhost:8000
```

Sign in with Google once.

## 8. Try the assistant

Say:

```text
Gary, add a dentist appointment Friday at 2 PM.
```

The local Whisper container detects the wake word first. Only after activation
does it stream the buffered pre-roll and live microphone audio to the backend.

## 9. Stop

```bash
docker compose down
```

Google refresh credentials remain encrypted in `data/token_store.enc`.
