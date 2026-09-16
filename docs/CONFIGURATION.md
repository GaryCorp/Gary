# Configuration Reference

Settings live in `.env`.

## Required secret

### `OPENAI_API_KEY`

Your OpenAI API key.

It is supplied to the backend container only.

## Generated secrets

### `SESSION_SECRET`

Signs FastAPI/Starlette session cookies.

### `TOKEN_ENCRYPTION_KEY`

Fernet key used to encrypt the local Google token store.

### `VOICE_BRIDGE_TOKEN`

Shared secret used by the voice service when it connects to the backend's
internal WebSocket.

## Host identity

### `HOST_UID`

UID used to build the non-root container users.

### `HOST_GID`

Primary GID used to build the non-root container users.

### `AUDIO_GID`

Host group ID that owns the sound devices.

## OpenAI

### `OPENAI_REALTIME_MODEL`

Default:

```text
gpt-realtime-2.1
```

### Voice (Piper TTS)

Gary's replies are generated as text by OpenAI Realtime and spoken locally by
the open-source [Piper](https://github.com/OHF-Voice/piper1-gpl) TTS engine.

#### `PIPER_VOICE`

Default:

```text
en_US-ryan-medium
```

Any voice from `rhasspy/piper-voices`, e.g. `en_US-lessac-medium` or
`en_GB-alan-medium`. Downloaded at build time, so rebuild after changing it:

```bash
docker compose build voice
docker compose up -d voice
```

#### `PIPER_LENGTH_SCALE`

Default: `1.0`. Higher is slower speech (e.g. `1.15`), lower is faster.

While Gary is speaking the microphone is muted, so you cannot interrupt him
mid-sentence; speak after he finishes.

## Time zone

### `LOCAL_TIMEZONE`

Default:

```text
America/Chicago
```

Use an IANA timezone name.

## Wake-word settings

### `WHISPER_MODEL`

Default:

```text
base.en
```

Downloaded during `docker compose up --build`. On a laptop CPU, `base.en`
takes about 1.1 s per wake check, `tiny` about 0.6 s, and `small.en` about 3 s
(GPU recommended). Keep `WAKE_CHECK_INTERVAL_SECONDS` above the check time.

### GPU (NVIDIA)

Whisper runs on CPU by default. To use an NVIDIA GPU, install the host driver
and `nvidia-container-toolkit`, then run:

```bash
make up-gpu
make gpu-check
```

This applies `compose.gpu.yaml`, which builds the voice image with CUDA 12
libraries and sets `WHISPER_DEVICE=cuda`. If CUDA fails to start, the voice
service logs a warning and falls back to CPU.

### `WHISPER_COMPUTE_TYPE`

GPU only. Default: `int8`. Newer GPUs (Turing or later) can use `float16`
or `int8_float16`.

### `WAKE_WORD`

Default:

```text
gary
```

Also accepts common Whisper spellings (Garry, Geary). Set `ai` for the
original wake word.

### `WAKE_WINDOW_SECONDS`

Default:

```text
2.2
```

Amount of recent audio given to local Whisper for wake detection.

### `PRE_ROLL_SECONDS`

Default:

```text
1.8
```

Amount of audio from immediately before activation that is forwarded once the
wake word is detected.

### `WAKE_CHECK_INTERVAL_SECONDS`

Default:

```text
1.25
```

How frequently the application invokes local Whisper while sleeping.

Lower values improve reaction speed but consume more CPU.

## Active conversation

### `ACTIVE_SESSION_SECONDS`

Default:

```text
45
```

Upper active-session window.

### `FOLLOWUP_GRACE_SECONDS`

Default:

```text
10
```

After a response finishes, the assistant remains active briefly so a follow-up
can be spoken without another wake word.

## New email check

### `EMAIL_CHECK_INTERVAL_MINUTES`

Default:

```text
60
```

How often the backend checks for new unread email in the Gmail Primary inbox.
When there is new email since the last check, Gary says how many, and the
sender and subject of up to three. No-reply senders are skipped. `0` turns the
check off.

The check runs only while the voice service is connected, and never contacts
OpenAI.

### `EMAIL_CHECK_QUIET_HOURS`

Default:

```text
22-7
```

Local hours, on a 24-hour clock, when Gary does not announce new email: `22-7`
means 10 PM to 7 AM. Email that arrives then is announced at the first check
afterwards. Leave empty to announce at any hour.

## Audio device

### `AUDIO_DEVICE`

Default: empty.

An empty value lets PortAudio choose its default device.

To list devices:

```bash
make audio-devices
```

Then set a numeric device index if necessary.
