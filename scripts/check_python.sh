#!/usr/bin/env bash
set -euo pipefail

python3 -m py_compile backend/app/main.py
python3 -m py_compile voice/voice_client.py

echo "Python syntax check passed."
