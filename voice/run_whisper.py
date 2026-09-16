"""Place beside voice_client.py; run with the project's virtualenv Python.

Example from the project root: .venv/bin/python voice/run_whisper.py
The backend must already be running on localhost:8000.
"""

import os
from pathlib import Path
import runpy
import sys
import urllib.request


VOICE_SETTINGS = (
    "VOICE_BRIDGE_TOKEN", "WAKE_WORD", "AUDIO_DEVICE",
    "PRE_ROLL_SECONDS", "WAKE_WINDOW_SECONDS",
    "WAKE_CHECK_INTERVAL_SECONDS", "ACTIVE_SESSION_SECONDS",
    "FOLLOWUP_GRACE_SECONDS",
)


def configure(root: Path, values: dict) -> Path:
    # Load only voice settings. Never load API or Google secrets into this process.
    for name in VOICE_SETTINGS:
        if values.get(name) is not None:
            os.environ[name] = values[name]
    if not os.environ.get("VOICE_BRIDGE_TOKEN", "").strip():
        raise RuntimeError("VOICE_BRIDGE_TOKEN is missing from the project .env.")

    model = values.get("WHISPER_MODEL") or "base.en"
    if model not in {"tiny", "tiny.en", "base", "base.en", "small", "small.en",
                     "medium", "medium.en", "large-v1", "large-v2", "large-v3"}:
        raise RuntimeError("Unsupported WHISPER_MODEL. Use base.en (default) or tiny.")
    model_path = root / ".models" / model
    os.environ["BACKEND_WS_URL"] = "ws://127.0.0.1:8000/internal/voice"
    os.environ["WHISPER_MODEL_PATH"] = str(model_path)
    return model_path


def check_backend() -> None:
    # A proxy is never needed for the local backend health check.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:8000/health", timeout=5) as response:
            if response.status != 200:
                raise RuntimeError("Backend health check failed.")
    except OSError as exc:
        raise RuntimeError(
            "Cannot reach the backend at localhost:8000. "
            "Start it with: docker compose up -d --no-deps backend"
        ) from exc


def main() -> None:
    voice_dir = Path(__file__).resolve().parent
    root = voice_dir.parent
    client = voice_dir / "voice_client.py"
    if not client.is_file():
        raise RuntimeError("Place run_whisper.py inside voice/, beside voice_client.py.")
    if not (root / ".env").is_file():
        raise RuntimeError("The project .env file is missing.")

    try:
        from dotenv import dotenv_values
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError(
            "Install dependencies in your virtualenv with: python -m pip install "
            "-r voice/requirements.txt python-dotenv"
        ) from exc

    model_path = configure(root, dotenv_values(root / ".env", interpolate=False))
    check_backend()

    required = ("model.bin", "config.json", "tokenizer.json")
    if not all((model_path / name).is_file() for name in required):
        print("Downloading Whisper model (first run requires internet)...", flush=True)
        snapshot_download(
            repo_id=f"Systran/faster-whisper-{model_path.name}",
            local_dir=str(model_path),
        )

    # Disable automatic WebSocket proxy discovery for this localhost connection.
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    print("Starting local voice assistant. Press Ctrl+C to stop.", flush=True)
    runpy.run_path(str(client), run_name="__main__")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception as exc:
        print(f"Whisper startup failed: {exc}", file=sys.stderr)
        sys.exit(1)
