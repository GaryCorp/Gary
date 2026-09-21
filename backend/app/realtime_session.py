"""One OpenAI Realtime connection for VOICE_MODE=realtime.

The turn-taking rules are in app.realtime, which has no I/O. This is the
connection itself: opened on wake, closed on sleep, renewed when it expires,
retried on a rate limit, and forwarding every event to the voice client.
"""

import asyncio
import base64
import json
import re

import websockets

from app.config import (
    OPENAI_API_KEY,
    OPENAI_REALTIME_MODEL,
    REALTIME_RATE_LIMIT_MAX_WAIT,
    REALTIME_RATE_LIMIT_RETRIES,
)
from app.realtime import ResponseGate, needs_follow_up
from gary.finance.usage import SpendCeilingReached

REALTIME_URL = "wss://api.openai.com/v1/realtime"


def session_payload(instructions: str, tools: list) -> dict:
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "model": OPENAI_REALTIME_MODEL,
            # Text only: the voice service speaks it with local Piper TTS.
            "output_modalities": ["text"],
            "instructions": instructions,
            "tools": tools,
            "tool_choice": "auto",
            "audio": {
                "input": {
                    "format": {
                        "type": "audio/pcm",
                        "rate": 24000,
                    },
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.5,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 650,
                        "create_response": True,
                        # Speaker echo must not cancel replies; the
                        # voice service mutes the mic while speaking.
                        "interrupt_response": False,
                    },
                },
            },
        },
    }


class RealtimeSession:
    """One OpenAI Realtime connection, opened on wake, closed on sleep.

    Realtime sessions expire after 60 minutes, so a connection held open
    for the life of the voice service is guaranteed to fail eventually,
    often mid-conversation. Connecting per activation keeps sessions short,
    avoids an idle upstream connection, and lets an expired one be
    replaced transparently.
    """

    def __init__(
        self,
        websocket,
        session: dict,
        *,
        instructions,
        tools: list,
        dispatch,
        spend_stop,
        on_usage,
        opening_context,
    ):
        # The voice client's websocket, and what this conversation has seen.
        self.websocket = websocket
        self.session = session
        # Called on every connect, so the date in the prompt stays current.
        self.instructions = instructions
        self.tools = tools
        # async (name, arguments_json, session) -> result
        self.dispatch = dispatch
        # async (what) -> gate state when the spend ceiling stops the call
        self.spend_stop = spend_stop
        # (response) -> None, for the usage ledger
        self.on_usage = on_usage
        # async (session) -> text Gary should open the session knowing, or None
        self.opening_context = opening_context
        self.connection = None
        self.reader = None
        self.connecting = asyncio.Lock()
        # Set when a session ended on its own (expiry or network drop)
        # rather than because the assistant went to sleep.
        self.dropped = False
        # Consecutive responses retried after an OpenAI rate limit.
        self.rate_limit_retries = 0
        # Whose turn it is: only one response may be active at a time.
        self.gate = ResponseGate()

    async def connect(self):
        async with self.connecting:
            if self.connection is not None:
                return self.connection

            # No model call may be made past the daily ceiling, and a
            # conversation is a model call.
            stopped = await self.spend_stop("voice")
            if stopped:
                raise SpendCeilingReached(stopped["reason"])

            connection = await websockets.connect(
                f"{REALTIME_URL}?model={OPENAI_REALTIME_MODEL}",
                additional_headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                max_size=None,
                ping_interval=20,
                ping_timeout=20,
            )
            # Fresh instructions each time, so the date stays current.
            await connection.send(
                json.dumps(session_payload(self.instructions(), self.tools))
            )
            await self.send_opening_context(connection)

            self.connection = connection
            self.reader = asyncio.create_task(
                self.forward_events(connection)
            )

            if self.dropped:
                self.dropped = False
                await self.websocket.send_text(
                    json.dumps(
                        {
                            "type": "bridge.notice",
                            "message": "OpenAI session renewed",
                        }
                    )
                )

            return connection

    async def forward_events(self, connection) -> None:
        try:
            async for raw in connection:
                event = json.loads(raw)
                self.gate.observe(event)
                # A follow-up rejected while nothing is active now would
                # otherwise never be retried, leaving a tool result unsaid.
                if event.get("type") == "error" and self.gate.take_pending():
                    await self.request_response(connection)

                # Preferred Realtime tool-call completion event.
                if event.get("type") == "response.function_call_arguments.done":
                    await self.send_tool_result(
                        connection,
                        event["call_id"],
                        event["name"],
                        event.get("arguments", "{}"),
                    )

                if event.get("type") == "response.done":
                    self.on_usage(event.get("response", {}))
                    await self.after_response(connection, event.get("response", {}))

                await self.websocket.send_text(raw)

        except websockets.exceptions.ConnectionClosed:
            pass  # Expired or dropped; the next audio reconnects.

        finally:
            # close() clears self.connection first, so still matching here
            # means the session ended on its own.
            if self.connection is connection:
                self.connection = None
                self.dropped = True

    async def send_opening_context(self, connection) -> None:
        text = await self.opening_context(self.session)
        if not text:
            return
        await connection.send(
            json.dumps(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    },
                }
            )
        )

    async def send_tool_result(
        self, connection, call_id: str, name: str, arguments_json: str
    ) -> None:
        """Run the tool and write its result back onto the session."""
        result = await self.dispatch(name, arguments_json, self.session)
        await connection.send(
            json.dumps(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(result),
                    },
                }
            )
        )
        # No response.create here: with several tool calls in one response, each
        # would start a new response while one is still active and be rejected.
        # The reader requests one follow-up response after response.done.

    async def request_response(self, connection) -> None:
        """Ask for a response, or queue the request when one is active.

        Server VAD starts its own responses, so a follow-up after a slow
        tool call can arrive while the user's new turn is being answered.
        Sending it anyway is rejected and the tool result is never spoken.
        """
        if self.gate.request():
            await connection.send(json.dumps({"type": "response.create"}))

    async def after_response(self, connection, response: dict) -> None:
        # Tool results were all sent, in order, as their calls arrived; ask
        # the model to continue once the response that made them is done.
        # A follow-up queued while another response was active is owed now.
        if needs_follow_up(response) or self.gate.take_pending():
            self.rate_limit_retries = 0
            await self.request_response(connection)
            return

        error = (response.get("status_details") or {}).get("error") or {}
        if response.get("status") != "failed" or error.get("code") != "rate_limit_exceeded":
            self.rate_limit_retries = 0
            return

        if self.rate_limit_retries >= REALTIME_RATE_LIMIT_RETRIES:
            self.rate_limit_retries = 0
            await self.websocket.send_text(
                json.dumps(
                    {
                        "type": "bridge.notice",
                        "message": "OpenAI rate limit reached; not retrying again",
                    }
                )
            )
            return

        self.rate_limit_retries += 1
        match = re.search(r"try again in ([\d.]+)s", error.get("message", ""))
        delay = min(float(match.group(1)) + 1 if match else 15, REALTIME_RATE_LIMIT_MAX_WAIT)
        await self.websocket.send_text(
            json.dumps(
                {
                    "type": "bridge.notice",
                    "message": f"OpenAI rate limit reached; retrying in {delay:.0f}s",
                }
            )
        )

        async def retry():
            await asyncio.sleep(delay)
            if self.connection is connection:
                try:
                    await self.request_response(connection)
                except websockets.exceptions.ConnectionClosed:
                    pass

        asyncio.create_task(retry())

    async def send_audio(self, chunk: bytes) -> None:
        message = json.dumps(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(chunk).decode("ascii"),
            }
        )

        for attempt in (1, 2):
            connection = await self.connect()

            try:
                await connection.send(message)
                return
            except websockets.exceptions.ConnectionClosed:
                if self.connection is connection:
                    self.connection = None
                    self.dropped = True

                if attempt == 2:
                    raise

    async def close(self) -> None:
        connection, self.connection = self.connection, None
        reader, self.reader = self.reader, None

        if connection is not None:
            await connection.close()

        if reader is not None:
            reader.cancel()

            try:
                await reader
            except (asyncio.CancelledError, Exception):
                pass
