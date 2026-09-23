"""How long the microphone stays open after the wake word.

The live system kept transcribing indefinitely: every utterance pushed the
idle window out again, so any speech in the room held the session open, and
in transcribe mode a finished reply never started the follow-up grace at all,
because that was wired to a Realtime-only event.
"""

import pytest

segmentation = pytest.importorskip(
    "segmentation", reason="the voice service is mounted at /voice by scripts/test.sh"
)

Session = segmentation.Session


def session(**overrides) -> "segmentation.Session":
    settings = {"idle_seconds": 30.0, "followup_seconds": 10.0, "max_seconds": 180.0}
    settings.update(overrides)
    return Session(**settings)


def test_the_session_ends_after_the_idle_window():
    s = session()
    s.wake(0)
    assert s.expired(29) is False
    assert s.expired(31) is True


def test_a_busy_room_cannot_hold_the_session_open_forever():
    """Every utterance refreshes the idle window, so only the hard deadline
    ends a session in a room where somebody keeps talking."""
    s = session()
    s.wake(0)
    for now in range(0, 200, 5):
        if s.expired(now):
            break
        s.heard_speech(now)
    assert s.expired(181) is True
    assert 180 <= now <= 185


def test_an_answer_shortens_the_session_to_the_follow_up_grace():
    s = session()
    s.wake(0)
    s.heard_speech(5)  # Alex speaks: idle window runs to 35
    s.replied(8)       # Gary answers at 8, so the session ends at 18
    assert s.expired(17) is False
    assert s.expired(19) is True


def test_a_follow_up_within_the_grace_keeps_the_conversation_going():
    s = session()
    s.wake(0)
    s.replied(8)
    s.heard_speech(12)  # answered inside the grace
    assert s.expired(19) is False
    assert s.expired(43) is True


def test_gary_talking_never_times_out_mid_reply():
    s = session()
    s.wake(0)
    for now in range(1, 60):  # a long answer, spoken aloud
        s.speaking(now)
        assert s.expired(now) is False
    assert s.expired(70) is True


def test_waking_again_starts_a_fresh_session():
    s = session()
    s.wake(0)
    assert s.expired(200) is True
    s.wake(200)
    assert s.expired(210) is False
    assert s.remaining(210) == 20.0


def test_an_asleep_session_never_expires():
    s = session()
    assert s.expired(10_000) is False
    s.wake(0)
    s.sleep()
    assert s.active is False
    assert s.expired(10_000) is False
