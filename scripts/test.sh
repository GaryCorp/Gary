#!/usr/bin/env bash
# Runs the backend tests in the backend image against temporary databases.
# Same as "make test", for machines without make.
set -euo pipefail

cd "$(dirname "$0")/.."

docker compose build backend
docker compose run --rm --no-deps -T \
  -v ./backend:/src:ro -v ./voice:/voice:ro -w /src -e PYTHONDONTWRITEBYTECODE=1 \
  backend sh -c "pip install --quiet --user -r requirements-dev.txt && python -m pytest -p no:cacheprovider -q tests"
