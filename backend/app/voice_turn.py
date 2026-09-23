"""A spoken turn without the Realtime API.

The Realtime API did four things in one websocket session: decide when Alex
stopped talking, transcribe him, run the model with tools, and stream text
back. It charged for audio tokens throughout, which is the expensive part of
running GaryCorp.

This replaces it with two ordinary HTTP calls:

    utterance audio -> transcription model -> words
    words + history + tools -> text model -> reply (Piper speaks it)

Segmentation moves to the voice container, which already runs Whisper for the
wake word and knows when the room went quiet. Conversation history moves here,
because a text model is stateless and Realtime was not.

Nothing about the tools changes: Tool.schema() already emits the flat shape
the Responses API takes, so the same definitions serve both paths.
"""

import asyncio
import json
import logging
import urllib.error
import urllib.request
import uuid

logger = logging.getLogger("gary.voice_turn")

RESPONSES_URL = "https://api.openai.com/v1/responses"
TRANSCRIPTIONS_URL = "https://api.openai.com/v1/audio/transcriptions"

# A turn that keeps calling tools without ever answering is a loop; this is
# what stops it costing money forever.
MAX_TOOL_ROUNDS = 6
# How much of a conversation is resent as history. Every turn pays input
# tokens for all of it, so it is bounded rather than growing all session.
MAX_HISTORY_ITEMS = 40


class VoiceTurnError(RuntimeError):
    """The turn could not be completed. Never a fabricated reply."""


def _post_json(url: str, body: dict, api_key: str, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise VoiceTurnError(f"OpenAI returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise VoiceTurnError(f"Could not reach OpenAI: {exc}") from exc


def _multipart(fields: dict[str, str], filename: str, audio: bytes) -> tuple[bytes, str]:
    """A multipart body for the audio upload, without pulling in a dependency."""
    boundary = f"----gary{uuid.uuid4().hex}"
    parts = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n"
            .encode()
        )
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{filename}\"\r\nContent-Type: audio/wav\r\n\r\n".encode()
    )
    parts.append(audio)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def reply_text(data: dict) -> str:
    """The words to speak, from a Responses payload."""
    chunks = []
    for item in data.get("output") or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if part.get("type") == "output_text" and part.get("text"):
                chunks.append(part["text"])
    return "\n".join(chunks).strip()


def tool_calls(data: dict) -> list[dict]:
    return [item for item in (data.get("output") or []) if item.get("type") == "function_call"]


class VoiceTurn:
    """One spoken exchange: audio in, words out."""

    def __init__(
        self,
        api_key: str,
        model: str,
        transcribe_model: str,
        instructions,
        tools: list[dict],
        dispatch,
        usage=None,
        timeout: float = 120.0,
        url: str = RESPONSES_URL,
        transcribe_url: str = TRANSCRIPTIONS_URL,
        post=None,
        upload=None,
    ):
        self.api_key = api_key
        self.model = model
        self.transcribe_model = transcribe_model
        # Callable so the date and outstanding questions are current each turn.
        self.instructions = instructions
        self.tools = tools
        # async (name, arguments_json, session) -> result dict
        self.dispatch = dispatch
        self.usage = usage
        self.timeout = timeout
        self.url = url
        self.transcribe_url = transcribe_url
        # Injected in tests so nothing reaches the network.
        self._post = post or _post_json
        self._upload = upload or self._upload_audio

    # ------------------------------------------------------- transcription

    def _upload_audio(self, audio: bytes) -> dict:
        body, content_type = _multipart(
            {"model": self.transcribe_model, "response_format": "json"},
            "utterance.wav",
            audio,
        )
        request = urllib.request.Request(
            self.transcribe_url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": content_type,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise VoiceTurnError(f"Transcription failed with HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise VoiceTurnError(f"Could not reach the transcription service: {exc}") from exc

    async def transcribe(self, audio: bytes, seconds: float) -> str:
        """Turn one utterance into words, and record what the audio cost."""
        data = await asyncio.to_thread(self._upload, audio)
        text = (data.get("text") or "").strip()
        self._record_audio(seconds, data)
        return text

    @staticmethod
    def transcription_usage(reported: dict, measured_seconds: float):
        """What the transcription actually cost.

        Models bill this endpoint two different ways and say which:
        ``{"type": "duration", "seconds": n}`` or ``{"type": "tokens", ...}``.
        The reported figure wins over the duration the voice service measured,
        because it is the one being charged; the measurement is only a
        fallback for a model that reports nothing.
        """
        from gary.finance.pricing import Usage, usage_from_openai

        kind = (reported or {}).get("type")
        if kind == "duration":
            return Usage(audio_seconds=float(reported.get("seconds") or 0.0))
        if kind == "tokens" or (reported and "input_tokens" in reported):
            return usage_from_openai(reported)
        return Usage(audio_seconds=round(measured_seconds, 2))

    def _record_audio(self, seconds: float, data: dict) -> None:
        if self.usage is None:
            return
        try:
            self.usage.record(
                "voice_transcription",
                data.get("model") or self.transcribe_model,
                self.transcription_usage(data.get("usage") or {}, seconds),
                detail="transcribing what Alex said",
                agent_id="gary",
            )
        except Exception:
            logger.exception("Could not record transcription usage")

    # ------------------------------------------------------------ the turn

    async def respond(self, session: dict, text: str) -> str:
        """Answer one transcript, running tools until the model has words.

        History lives in ``session`` and is cleared when Gary sleeps, which is
        the same lifetime the Realtime conversation had.
        """
        history = session.setdefault("messages", [])
        history.append({"role": "user", "content": text})

        for _ in range(MAX_TOOL_ROUNDS):
            data = await asyncio.to_thread(
                self._post,
                self.url,
                {
                    "model": self.model,
                    "instructions": self.instructions(),
                    "input": history[-MAX_HISTORY_ITEMS:],
                    "tools": self.tools,
                    "store": False,
                },
                self.api_key,
                self.timeout,
            )
            self._record_turn(data)

            calls = tool_calls(data)
            if not calls:
                spoken = reply_text(data)
                if spoken:
                    history.append({"role": "assistant", "content": spoken})
                return spoken

            # Keep the calls in history so the model sees what it asked for.
            history.extend(calls)
            for call in calls:
                result = await self.dispatch(
                    call.get("name", ""), call.get("arguments", "{}"), session
                )
                history.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.get("call_id"),
                        "output": json.dumps(result),
                    }
                )

        raise VoiceTurnError(
            f"Gave up after {MAX_TOOL_ROUNDS} rounds of tool calls without an answer"
        )

    def _record_turn(self, data: dict) -> None:
        if self.usage is None:
            return
        from gary.finance.pricing import usage_from_openai

        reported = data.get("usage")
        if not reported:
            return
        try:
            self.usage.record(
                "voice",
                data.get("model") or self.model,
                usage_from_openai(reported),
                entity_type="voice_turn",
                entity_id=str(data.get("id") or ""),
                detail="voice conversation",
                agent_id="gary",
            )
        except Exception:
            logger.exception("Could not record voice usage")


__all__ = ["VoiceTurn", "VoiceTurnError", "reply_text", "tool_calls", "MAX_TOOL_ROUNDS"]
