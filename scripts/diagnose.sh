#!/usr/bin/env bash
set -u

echo "=== Docker ==="
docker --version 2>/dev/null || true
docker compose version 2>/dev/null || true

echo
echo "=== Host identity ==="
id

echo
echo "=== /dev/snd ==="
ls -l /dev/snd 2>/dev/null || echo "/dev/snd not found"

echo
echo "=== audio group ==="
getent group audio 2>/dev/null || true

echo
echo "=== .env present ==="
if [ -f .env ]; then
  echo ".env exists"
  grep -E '^(HOST_UID|HOST_GID|AUDIO_GID|WHISPER_MODEL|AUDIO_DEVICE|LOCAL_TIMEZONE)=' .env || true
else
  echo ".env does not exist"
fi

echo
echo "=== client_secret.json ==="
if [ -f client_secret.json ]; then
  echo "client_secret.json exists"
else
  echo "client_secret.json is missing"
fi

echo
echo "=== Compose configuration ==="
docker compose config --quiet 2>/dev/null \
  && echo "Compose config: OK" \
  || echo "Compose config: FAILED"

echo
echo "=== Container status ==="
docker compose ps 2>/dev/null || true

echo
echo "=== Voice audio devices ==="
docker compose run --rm voice \
  python -c "import sounddevice as sd; print(sd.query_devices())" \
  2>/dev/null || echo "Could not query audio devices"
