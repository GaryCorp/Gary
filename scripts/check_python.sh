#!/usr/bin/env bash
set -euo pipefail

python3 -m py_compile backend/app/main.py
find backend/gary backend/tests -name '*.py' -exec python3 -m py_compile {} +
python3 -m py_compile voice/voice_client.py
python3 -m py_compile joplin_proxy/joplin_proxy.py

echo "Python syntax check passed."
