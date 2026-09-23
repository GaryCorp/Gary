"""Deciding when Alex has finished speaking.

The Realtime API used to decide this server-side with its own VAD. With
transcription nothing does, so it is decided here: collect blocks while the
room is loud, and call the utterance finished once it has been quiet for long
enough.

Deliberately free of numpy and of the audio stack, so the rule can be tested
without a microphone, a model or a sound card. The caller measures each
block's energy and hands the number in; this only decides what that means.
"""

# How much quiet ends an utterance.
DEFAULT_SILENCE_SECONDS = 1.2
# Below this the room counts as quiet.
DEFAULT_ENERGY = 0.006
# Nothing sensible is one utterance for longer, and an open microphone must
# not become an unbounded upload.
DEFAULT_MAX_SECONDS = 30.0


class Utterance:
    """What Alex is saying, until he stops.

    Leading silence is dropped, so an open microphone with nobody talking
    collects nothing and sends nothing.
    """

    def __init__(
        self,
        block_ms: int,
        silence_seconds: float = DEFAULT_SILENCE_SECONDS,
        energy_threshold: float = DEFAULT_ENERGY,
        max_seconds: float = DEFAULT_MAX_SECONDS,
    ):
        self.block_seconds = block_ms / 1000.0
        self.silence_seconds = silence_seconds
        self.energy_threshold = energy_threshold
        self.max_seconds = max_seconds
        self.blocks: list = []
        self.quiet_blocks = 0
        self.heard_speech = False

    @property
    def seconds(self) -> float:
        return len(self.blocks) * self.block_seconds

    @property
    def empty(self) -> bool:
        return not self.blocks

    def add(self, block, energy: float) -> bool:
        """Add one block of audio. True when the utterance is complete."""
        loud = energy >= self.energy_threshold

        if not self.heard_speech:
            if not loud:
                return False  # still waiting for him to start
            self.heard_speech = True

        self.blocks.append(block)
        self.quiet_blocks = 0 if loud else self.quiet_blocks + 1

        quiet_for = self.quiet_blocks * self.block_seconds
        return quiet_for >= self.silence_seconds or self.seconds >= self.max_seconds

    def take(self) -> list:
        """The blocks collected, leaving the utterance ready for the next one."""
        blocks, self.blocks = self.blocks, []
        self.quiet_blocks = 0
        self.heard_speech = False
        return blocks

    def reset(self) -> None:
        self.take()


__all__ = [
    "Utterance",
    "DEFAULT_SILENCE_SECONDS",
    "DEFAULT_ENERGY",
    "DEFAULT_MAX_SECONDS",
]


# How long the microphone stays open with nothing happening.
DEFAULT_IDLE_SECONDS = 30.0
# After Gary answers, how long Alex has to follow up without the wake word.
DEFAULT_FOLLOWUP_SECONDS = 10.0
# The hard end of one activation, however busy the room is. Without it every
# utterance pushes the idle window out again, and a room with a television in
# it keeps the microphone open (and transcription billing) indefinitely.
DEFAULT_MAX_SESSION_SECONDS = 180.0


class Session:
    """How long the microphone stays open after the wake word.

    Two clocks, and the earlier one wins:

    * an idle window, refreshed by things that mean the conversation is still
      going (Alex speaking, waiting for an answer, Gary talking), shortened to
      the follow-up grace once Gary has answered; and
    * a hard deadline set when the wake word was heard, which nothing
      refreshes.

    The hard deadline is the point. It is what stops a noisy room holding the
    session open forever, one utterance at a time.

    Free of the audio stack: the caller passes a monotonic ``now``.
    """

    def __init__(
        self,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
        followup_seconds: float = DEFAULT_FOLLOWUP_SECONDS,
        max_seconds: float = DEFAULT_MAX_SESSION_SECONDS,
    ):
        self.idle_seconds = idle_seconds
        self.followup_seconds = followup_seconds
        self.max_seconds = max_seconds
        self.active = False
        self.idle_deadline = 0.0
        self.hard_deadline = 0.0

    def wake(self, now: float) -> None:
        self.active = True
        self.idle_deadline = now + self.idle_seconds
        self.hard_deadline = now + self.max_seconds

    def sleep(self) -> None:
        self.active = False
        self.idle_deadline = 0.0
        self.hard_deadline = 0.0

    def heard_speech(self, now: float) -> None:
        """Alex is talking, or his words are on their way to be answered."""
        self.idle_deadline = max(self.idle_deadline, now + self.idle_seconds)

    def speaking(self, now: float) -> None:
        """Gary is talking: never time out mid-reply, and leave the grace
        running from the moment he stops."""
        self.idle_deadline = max(self.idle_deadline, now + self.followup_seconds)

    def replied(self, now: float) -> None:
        """Gary has answered. The session now ends after the short follow-up
        grace, not after another full idle window."""
        self.idle_deadline = now + self.followup_seconds

    def expired(self, now: float) -> bool:
        return self.active and (now > self.idle_deadline or now > self.hard_deadline)

    def remaining(self, now: float) -> float:
        return max(0.0, min(self.idle_deadline, self.hard_deadline) - now)
