#!/usr/bin/env bash
set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: Docker is not installed or is not in PATH."
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "ERROR: Docker Compose v2 is required."
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
fi

python3 - <<'PY'
from pathlib import Path
import base64
import os
import secrets

path = Path(".env")
lines = path.read_text().splitlines()

values = {}
for line in lines:
    if "=" in line and not line.lstrip().startswith("#"):
        key, value = line.split("=", 1)
        values[key] = value

generated = {
    "SESSION_SECRET": secrets.token_hex(32),
    "TOKEN_ENCRYPTION_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
    "VOICE_BRIDGE_TOKEN": secrets.token_urlsafe(48),
    "HOST_UID": str(os.getuid()),
    "HOST_GID": str(os.getgid()),
}

# Placeholders from .env.example count as unset.
PLACEHOLDER = "generate_by_setup_sh"
ALWAYS_DETECT = {"HOST_UID", "HOST_GID"}

for key, value in generated.items():
    if key in ALWAYS_DETECT or values.get(key, "") in ("", PLACEHOLDER):
        values[key] = value

out = []
seen = set()
for line in lines:
    if "=" in line and not line.lstrip().startswith("#"):
        key, _ = line.split("=", 1)
        if key in values:
            out.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            out.append(line)
    else:
        out.append(line)

for key, value in values.items():
    if key not in seen:
        out.append(f"{key}={value}")

path.write_text("\n".join(out) + "\n")
PY

AUDIO_GID="$(getent group audio | cut -d: -f3 || true)"
if [ -z "${AUDIO_GID}" ] && [ -e /dev/snd/controlC0 ]; then
  AUDIO_GID="$(stat -c '%g' /dev/snd/controlC0 || true)"
fi
if [ -z "${AUDIO_GID}" ]; then
  AUDIO_GID=29
fi

python3 - "$AUDIO_GID" <<'PY'
from pathlib import Path
import sys

gid = sys.argv[1]
path = Path(".env")
lines = path.read_text().splitlines()

out = []
found = False
for line in lines:
    if line.startswith("AUDIO_GID="):
        out.append(f"AUDIO_GID={gid}")
        found = True
    else:
        out.append(line)

if not found:
    out.append(f"AUDIO_GID={gid}")

path.write_text("\n".join(out) + "\n")
PY

mkdir -p data
chmod 700 data
chmod 600 .env

echo
echo "Local configuration created."
echo
echo "Next:"
echo "  1. Put Google OAuth JSON at ./client_secret.json"
echo "  2. Edit .env and set OPENAI_API_KEY"
echo "  3. Run: docker compose build"
echo "  4. Run: docker compose up -d"
echo "  5. Open: http://localhost:8000"
