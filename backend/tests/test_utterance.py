"""Deciding when Alex has stopped talking.

The Realtime API did this server-side. Nothing does now, so this rule is the
difference between Gary answering and Gary listening forever. It runs in the
voice container, but the logic is deliberately free of numpy and the audio
stack so it can be tested here.
"""

import pytest

segmentation = pytest.importorskip(
    "segmentation", reason="the voice service is mounted at /voice by scripts/test.sh"
)
Utterance = segmentation.Utterance

BLOCK_MS = 100
LOUD = 0.05
QUIET = 0.0


def utterance(**overrides) -> Utterance:
    options = {"silence_seconds": 1.0, "energy_threshold": 0.006, "max_seconds": 5.0}
    options.update(overrides)
    return Utterance(BLOCK_MS, **options)


def speak(u: Utterance, blocks: int, energy: float = LOUD) -> bool:
    """Feed blocks; True if the utterance finished during them."""
    for i in range(blocks):
        if u.add(f"block-{i}", energy):
            return True
    return False


def test_silence_alone_collects_nothing():
    """An open microphone in an empty room must not send an utterance."""
    u = utterance()

    assert speak(u, blocks=50, energy=QUIET) is False
    assert u.empty is True
    assert u.seconds == 0


def test_an_utterance_ends_after_the_quiet_window():
    u = utterance(silence_seconds=1.0)

    assert speak(u, blocks=5, energy=LOUD) is False, "still talking"
    # Ten quiet blocks is one second.
    assert speak(u, blocks=9, energy=QUIET) is False
    assert u.add("last", QUIET) is True


def test_a_short_pause_does_not_end_it():
    u = utterance(silence_seconds=1.0)
    speak(u, blocks=5, energy=LOUD)

    assert speak(u, blocks=5, energy=QUIET) is False, "half a second is a breath"
    assert speak(u, blocks=5, energy=LOUD) is False, "and he carries on"
    assert u.seconds == pytest.approx(1.5)


def test_an_endless_utterance_is_capped():
    """An open microphone must not become an unbounded upload."""
    u = utterance(max_seconds=2.0)

    assert speak(u, blocks=100, energy=LOUD) is True
    assert u.seconds == pytest.approx(2.0)


def test_leading_silence_is_dropped():
    u = utterance()
    speak(u, blocks=20, energy=QUIET)

    u.add("first real block", LOUD)

    assert u.seconds == pytest.approx(0.1), "only what he actually said"


def test_taking_the_audio_leaves_it_ready_for_the_next_turn():
    u = utterance(silence_seconds=0.2)
    speak(u, blocks=3, energy=LOUD)
    u.add("trailing", QUIET)
    u.add("trailing", QUIET)

    blocks = u.take()

    assert len(blocks) == 5
    assert u.empty is True
    assert u.seconds == 0
    # And the next utterance again waits for speech before collecting.
    assert u.add("quiet", QUIET) is False
    assert u.empty is True
