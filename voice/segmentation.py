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
