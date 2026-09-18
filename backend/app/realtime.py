"""Realtime response turn-taking.

The Realtime API allows one active response per conversation. Two things ask
for a response: OpenAI's server VAD, which starts one on its own when the user
stops speaking, and Gary, who asks for one after a tool call so the model can
speak the tool's result.

Those two race whenever a tool takes a while: the user says something during
the call, VAD starts a response, and Gary's follow-up request is then rejected
with ``conversation_already_has_active_response`` — leaving the tool result
unspoken.

``ResponseGate`` tracks whether a response is active and defers the follow-up
until the active one finishes, so the request is queued rather than lost. It is
plain state with no I/O, so the turn-taking rules can be tested directly.
"""

ACTIVE_RESPONSE_ERROR = "conversation_already_has_active_response"


class ResponseGate:
    def __init__(self):
        self.active = False
        # A follow-up response owed to a finished tool call.
        self.pending = False

    def observe(self, event: dict) -> None:
        """Track the Realtime events that change whose turn it is."""
        event_type = event.get("type")
        if event_type == "response.created":
            self.active = True
        elif event_type == "response.done":
            self.active = False
        elif event_type == "error":
            if (event.get("error") or {}).get("code") == ACTIVE_RESPONSE_ERROR:
                # Something else is speaking; ask again when it finishes.
                self.pending = True

    def request(self) -> bool:
        """Ask for a response. True means send ``response.create`` now; False
        means one is already active and the request was queued."""
        if self.active:
            self.pending = True
            return False
        self.pending = False
        return True

    def take_pending(self) -> bool:
        """True when a queued follow-up is now due (called on response.done)."""
        if self.pending and not self.active:
            self.pending = False
            return True
        return False


def needs_follow_up(response: dict) -> bool:
    """True when a finished response made tool calls, so the model has tool
    output waiting and should continue."""
    return any(item.get("type") == "function_call" for item in response.get("output", []))
