import asyncio
import json
import math
import os
import queue
import re
import threading
import time
from collections import deque

import numpy as np
import sounddevice as sd
import websockets
from faster_whisper import WhisperModel
from piper import PiperVoice, SynthesisConfig


BACKEND_WS_URL = os.getenv(
    "BACKEND_WS_URL",
    "ws://backend:8000/internal/voice",
)
VOICE_BRIDGE_TOKEN = os.environ["VOICE_BRIDGE_TOKEN"]
WHISPER_MODEL_PATH = os.getenv(
    "WHISPER_MODEL_PATH",
    "/models/base.en",
)

WAKE_WORD = os.getenv("WAKE_WORD", "gary").lower()

# Common Whisper renderings of each spoken wake word.
WAKE_ALIASES = {
    "ai": {"ai", "eye", "aye"},
    "gary": {"gary", "garry", "geary", "garey", "gari"},
}

WAKE_WORD_DISPLAY = (
    "AI" if WAKE_WORD == "ai" else WAKE_WORD.title()
)

SAMPLE_RATE = 24000
WHISPER_RATE = 16000
CHANNELS = 1

BLOCK_MS = 100
BLOCK_SAMPLES = SAMPLE_RATE * BLOCK_MS // 1000

PRE_ROLL_SECONDS = float(
    os.getenv("PRE_ROLL_SECONDS", "1.8")
)
WAKE_WINDOW_SECONDS = float(
    os.getenv("WAKE_WINDOW_SECONDS", "2.2")
)
WAKE_CHECK_INTERVAL_SECONDS = float(
    os.getenv("WAKE_CHECK_INTERVAL_SECONDS", "1.25")
)

ACTIVE_SESSION_SECONDS = float(
    os.getenv("ACTIVE_SESSION_SECONDS", "45")
)
FOLLOWUP_GRACE_SECONDS = float(
    os.getenv("FOLLOWUP_GRACE_SECONDS", "10")
)

PIPER_VOICE_PATH = os.getenv(
    "PIPER_VOICE_PATH",
    "/models/piper/en_US-ryan-medium.onnx",
)
# >1.0 speaks slower, <1.0 faster.
PIPER_LENGTH_SCALE = float(
    os.getenv("PIPER_LENGTH_SCALE", "1.0")
)

# Keep the mic muted briefly after playback so room echo is not sent.
SPEAKING_TAIL_SECONDS = 0.4

AUDIO_DEVICE_RAW = os.getenv(
    "AUDIO_DEVICE",
    "",
).strip()

PRE_ROLL_BLOCKS = max(
    1,
    math.ceil(
        PRE_ROLL_SECONDS * 1000 / BLOCK_MS
    ),
)

WAKE_WINDOW_BLOCKS = max(
    1,
    math.ceil(
        WAKE_WINDOW_SECONDS * 1000 / BLOCK_MS
    ),
)

audio_queue: queue.Queue[np.ndarray] = queue.Queue(
    maxsize=100
)
tts_queue: queue.Queue[str] = queue.Queue()


class PlaybackBuffer:
    """PCM16 bytes for the speaker callback. Never drops audio."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data = bytearray()
        self.last_audio = 0.0

    def write(self, pcm: bytes) -> None:
        with self._lock:
            self._data.extend(pcm)

    def read(self, size: int) -> bytes:
        with self._lock:
            chunk = bytes(self._data[:size])
            del self._data[:size]

        if chunk:
            self.last_audio = time.monotonic()

        return chunk + b"\x00" * (size - len(chunk))

    def pending(self) -> bool:
        with self._lock:
            return len(self._data) > 0


playback = PlaybackBuffer()


def parse_device(value: str):
    if not value or value.lower() == "default":
        return None

    try:
        return int(value)
    except ValueError:
        return value


AUDIO_DEVICE = parse_device(AUDIO_DEVICE_RAW)


def input_callback(
    indata,
    frames,
    time_info,
    status,
):
    if status:
        print(
            f"[mic] {status}",
            flush=True,
        )

    try:
        audio_queue.put_nowait(
            indata[:, 0].copy()
        )
    except queue.Full:
        pass


def output_callback(
    outdata,
    frames,
    time_info,
    status,
):
    if status:
        print(
            f"[speaker] {status}",
            flush=True,
        )

    samples = np.frombuffer(
        playback.read(frames * 2),
        dtype=np.int16,
    )

    outdata[:, 0] = samples


def pcm16_bytes(
    audio: np.ndarray,
) -> bytes:
    clipped = np.clip(
        audio,
        -1.0,
        1.0,
    )

    return (
        clipped * 32767.0
    ).astype("<i2").tobytes()


def resample_24k_to_16k(
    audio: np.ndarray,
) -> np.ndarray:
    if len(audio) == 0:
        return audio.astype(np.float32)

    target_len = max(
        1,
        round(
            len(audio)
            * WHISPER_RATE
            / SAMPLE_RATE
        ),
    )

    old_x = np.linspace(
        0.0,
        1.0,
        num=len(audio),
        endpoint=False,
    )

    new_x = np.linspace(
        0.0,
        1.0,
        num=target_len,
        endpoint=False,
    )

    return np.interp(
        new_x,
        old_x,
        audio,
    ).astype(np.float32)


def normalize_words(
    text: str,
) -> list[str]:
    cleaned = re.sub(
        r"[^a-z0-9]+",
        " ",
        text.lower(),
    )

    return cleaned.split()


def wake_detected(
    text: str,
) -> bool:
    words = normalize_words(text)
    accepted = WAKE_ALIASES.get(
        WAKE_WORD,
        {WAKE_WORD},
    )

    return any(
        word in accepted
        for word in words
    )


def microphone_energy(
    audio: np.ndarray,
) -> float:
    if len(audio) == 0:
        return 0.0

    return float(
        np.sqrt(
            np.mean(
                np.square(audio),
                dtype=np.float64,
            )
        )
    )


WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu").lower()
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")


def load_whisper() -> WhisperModel:
    print(
        f"Loading local Whisper from {WHISPER_MODEL_PATH} "
        f"on {WHISPER_DEVICE} ({WHISPER_COMPUTE_TYPE}) ...",
        flush=True,
    )

    try:
        model = WhisperModel(
            WHISPER_MODEL_PATH,
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE_TYPE,
            local_files_only=True,
        )

        # CUDA library problems often surface only on first inference.
        model.transcribe(
            np.zeros(WHISPER_RATE, dtype=np.float32),
            language="en",
            beam_size=1,
        )
        return model

    except Exception as exc:
        if WHISPER_DEVICE == "cpu":
            raise

        print(
            f"[whisper] {WHISPER_DEVICE} unavailable ({exc}); "
            "falling back to CPU int8.",
            flush=True,
        )

        return WhisperModel(
            WHISPER_MODEL_PATH,
            device="cpu",
            compute_type="int8",
            local_files_only=True,
        )


whisper = load_whisper()

print(
    "Whisper ready.",
    flush=True,
)

print(
    f"Loading Piper voice from {PIPER_VOICE_PATH} ...",
    flush=True,
)

piper_voice = PiperVoice.load(PIPER_VOICE_PATH)
piper_config = SynthesisConfig(
    length_scale=PIPER_LENGTH_SCALE,
)
TTS_SAMPLE_RATE = piper_voice.config.sample_rate

print(
    f"Piper ready ({TTS_SAMPLE_RATE} Hz).",
    flush=True,
)


def tts_worker() -> None:
    while True:
        text = tts_queue.get()

        try:
            for chunk in piper_voice.synthesize(
                text,
                syn_config=piper_config,
            ):
                playback.write(
                    chunk.audio_int16_bytes
                )
        except Exception as exc:
            print(
                f"[tts] {exc}",
                flush=True,
            )
        finally:
            tts_queue.task_done()


threading.Thread(
    target=tts_worker,
    daemon=True,
).start()


def speak(text: str) -> None:
    cleaned = re.sub(r"[*_#`~>|]", "", text).strip()

    if cleaned:
        tts_queue.put(cleaned)


def assistant_speaking() -> bool:
    return (
        tts_queue.unfinished_tasks > 0
        or playback.pending()
        or time.monotonic() - playback.last_audio
        < SPEAKING_TAIL_SECONDS
    )


# Sentence end: punctuation followed by whitespace.
SENTENCE_BREAK = re.compile(r"[.!?]\s")


def speak_complete_sentences(
    state: dict,
    final: bool = False,
) -> None:
    pending = state["tts_text"]

    if final:
        cut = len(pending)
    else:
        matches = list(SENTENCE_BREAK.finditer(pending))
        if not matches:
            return
        cut = matches[-1].end()

    speak(pending[:cut])
    state["tts_text"] = pending[cut:]


def transcribe_for_wake(
    audio_24k: np.ndarray,
) -> str:
    audio_16k = resample_24k_to_16k(
        audio_24k
    )

    segments, _ = whisper.transcribe(
        audio_16k,
        language="en",
        beam_size=1,
        best_of=1,
        temperature=0.0,
        vad_filter=True,
        condition_on_previous_text=False,
        without_timestamps=True,
    )

    return " ".join(
        segment.text.strip()
        for segment in segments
    ).strip()


async def receiver(
    websocket,
    state: dict,
):
    async for raw in websocket:
        event = json.loads(raw)
        event_type = event.get("type")

        if event_type == "response.output_text.delta":
            text = event.get(
                "delta",
                "",
            )

            if text:
                print(
                    text,
                    end="",
                    flush=True,
                )

                state["tts_text"] += text
                speak_complete_sentences(state)

        elif event_type == "response.output_text.done":
            speak_complete_sentences(
                state,
                final=True,
            )

        elif event_type == "response.done":
            speak_complete_sentences(
                state,
                final=True,
            )

            print(flush=True)

            now = time.monotonic()
            state["last_response_done"] = now
            state["followup_deadline"] = (
                now + FOLLOWUP_GRACE_SECONDS
            )

        elif event_type == "input_audio_buffer.speech_started":
            now = time.monotonic()

            state["hard_deadline"] = max(
                state["hard_deadline"],
                now + ACTIVE_SESSION_SECONDS,
            )

        elif event_type == "error":
            print(
                f"\n[OpenAI error] {json.dumps(event)}",
                flush=True,
            )

        elif event_type == "bridge.announce":
            message = event.get("message", "")
            print(
                f"\n[new email] {message}",
                flush=True,
            )

            if state["active"]:
                # Don't talk over a conversation; speak it on going to sleep.
                state["announcements"].append(message)
            else:
                speak(message)

        elif event_type == "bridge.notice":
            print(
                f"\n[{event.get('message')}]",
                flush=True,
            )

        elif event_type == "bridge.error":
            print(
                f"\n[bridge error] {event.get('message')}",
                flush=True,
            )


async def run():
    headers = {
        "Authorization": (
            f"Bearer {VOICE_BRIDGE_TOKEN}"
        )
    }

    while True:
        receiver_task = None

        try:
            async with websockets.connect(
                BACKEND_WS_URL,
                additional_headers=headers,
                max_size=None,
                ping_interval=20,
                ping_timeout=20,
            ) as websocket:

                print(
                    "Connected to backend. "
                    f"Listening locally for wake word: {WAKE_WORD_DISPLAY}",
                    flush=True,
                )

                state = {
                    "active": False,
                    "hard_deadline": 0.0,
                    "followup_deadline": 0.0,
                    "last_response_done": 0.0,
                    "tts_text": "",
                    "announcements": [],
                }

                receiver_task = asyncio.create_task(
                    receiver(
                        websocket,
                        state,
                    )
                )

                pre_roll = deque(
                    maxlen=PRE_ROLL_BLOCKS
                )

                wake_window = deque(
                    maxlen=WAKE_WINDOW_BLOCKS
                )

                next_wake_check = (
                    time.monotonic()
                )

                with sd.InputStream(
                    samplerate=SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="float32",
                    blocksize=BLOCK_SAMPLES,
                    device=AUDIO_DEVICE,
                    callback=input_callback,
                ), sd.OutputStream(
                    samplerate=TTS_SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=TTS_SAMPLE_RATE * BLOCK_MS // 1000,
                    device=AUDIO_DEVICE,
                    callback=output_callback,
                ):
                    while True:

                        if receiver_task.done():
                            exc = receiver_task.exception()

                            if exc:
                                raise exc

                            raise RuntimeError(
                                "Backend receiver stopped"
                            )

                        audio = await asyncio.to_thread(
                            audio_queue.get
                        )

                        now = time.monotonic()

                        if state["active"]:
                            if assistant_speaking():
                                # Half-duplex: keep Gary's own voice out of
                                # the mic stream, and don't time out mid-reply.
                                if state["followup_deadline"] > 0:
                                    state["followup_deadline"] = (
                                        now + FOLLOWUP_GRACE_SECONDS
                                    )

                                state["hard_deadline"] = max(
                                    state["hard_deadline"],
                                    now + FOLLOWUP_GRACE_SECONDS,
                                )
                                continue

                            await websocket.send(
                                pcm16_bytes(audio)
                            )

                            hard_expired = (
                                now
                                > state["hard_deadline"]
                            )

                            followup_expired = (
                                state["followup_deadline"] > 0
                                and now
                                > state["followup_deadline"]
                            )

                            if (
                                hard_expired
                                or followup_expired
                            ):
                                state["active"] = False
                                state["followup_deadline"] = 0.0

                                pre_roll.clear()
                                wake_window.clear()

                                await websocket.send(
                                    json.dumps(
                                        {
                                            "type": "bridge.reset"
                                        }
                                    )
                                )

                                print(
                                    f"\n[assistant sleeping — say {WAKE_WORD_DISPLAY} to wake]",
                                    flush=True,
                                )

                                for message in state["announcements"]:
                                    speak(message)
                                state["announcements"].clear()

                            continue

                        if assistant_speaking():
                            # An announcement can say the wake word (a sender
                            # named Gary); don't let Gary wake himself up.
                            pre_roll.clear()
                            wake_window.clear()
                            continue

                        pre_roll.append(
                            audio.copy()
                        )

                        wake_window.append(
                            audio.copy()
                        )

                        if (
                            now < next_wake_check
                            or len(wake_window)
                            < WAKE_WINDOW_BLOCKS
                        ):
                            continue

                        next_wake_check = (
                            now
                            + WAKE_CHECK_INTERVAL_SECONDS
                        )

                        window = np.concatenate(
                            list(wake_window)
                        )

                        # Skip near-silence to save CPU.
                        if microphone_energy(window) < 0.006:
                            continue

                        text = await asyncio.to_thread(
                            transcribe_for_wake,
                            window,
                        )

                        if text:
                            print(
                                f"[local] {text}",
                                flush=True,
                            )

                        if wake_detected(text):
                            state["active"] = True

                            state["hard_deadline"] = (
                                now
                                + ACTIVE_SESSION_SECONDS
                            )

                            state["followup_deadline"] = 0.0

                            print(
                                f"[{WAKE_WORD_DISPLAY} activated — streaming pre-roll + live audio]",
                                flush=True,
                            )

                            buffered = np.concatenate(
                                list(pre_roll)
                            )

                            await websocket.send(
                                pcm16_bytes(buffered)
                            )

        except Exception as exc:
            print(
                f"[voice] {exc}; reconnecting...",
                flush=True,
            )

            await asyncio.sleep(3)

        finally:
            # Stop the backend listener; otherwise its closed-connection error
            # surfaces later as an unretrieved task exception.
            if receiver_task is not None:
                receiver_task.cancel()

                try:
                    await receiver_task
                except (asyncio.CancelledError, Exception):
                    pass


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print(
            "Stopped.",
            flush=True,
        )
