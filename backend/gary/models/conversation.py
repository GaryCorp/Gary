"""What Gary says to Alex when he decides to speak first.

The text is spoken by local Piper TTS, so it is plain words: no markdown, no
lists, no symbols. That is enforced here rather than trusted to the prompt.
"""

import re

from pydantic import Field, field_validator

from gary.models.common import EntityId, RequestModel

MESSAGE_MIN = 10
MESSAGE_MAX = 600

# Markdown and anything else Piper would read out as punctuation noise.
SPOKEN_STRIP = re.compile(r"[*_#`~>|\[\]{}]")

URGENCY = ("now", "next_time")

# Which part of the company decided to speak. Set by Python at every call
# site -- the dedicated tool, or the planning cycle -- never chosen by the
# model, so an audit trail says where a message really came from.
SOURCES = (
    "voice",
    "planning_cycle",
    "management_loop",
    "approval",
    "email",
    "operations",
    "briefing",
)


def spoken_text(value: str) -> str:
    """One line of plain speech."""
    return " ".join(SPOKEN_STRIP.sub("", value).split())


class AskUserPayload(RequestModel):
    """Say something to Alex, and optionally wait for his answer.

    Green: speaking changes nothing by itself. What it costs is Alex's
    attention, which ConversationService caps.
    """

    message: str = Field(min_length=MESSAGE_MIN, max_length=MESSAGE_MAX)
    # A question stays open until Alex answers it, which he does by saying
    # the wake word in his own time; a notice is Gary keeping Alex informed
    # and closes itself.
    expects_reply: bool = True
    # "now" is said at the next opportunity; "next_time" waits for the next
    # conversation. Neither overrides quiet hours, and neither opens the
    # microphone: nothing does but the wake word.
    urgency: str = "now"
    source: str = "voice"
    approval_id: EntityId | None = None
    project_id: EntityId | None = None
    task_id: EntityId | None = None

    @field_validator("message")
    @classmethod
    def plain_speech(cls, value: str) -> str:
        spoken = spoken_text(value)
        if len(spoken) < MESSAGE_MIN:
            raise ValueError(f"must be at least {MESSAGE_MIN} characters of plain speech")
        return spoken

    @field_validator("urgency")
    @classmethod
    def known_urgency(cls, value: str) -> str:
        if value not in URGENCY:
            raise ValueError(f"must be one of {', '.join(URGENCY)}")
        return value

    @field_validator("source")
    @classmethod
    def known_source(cls, value: str) -> str:
        if value not in SOURCES:
            raise ValueError(f"must be one of {', '.join(SOURCES)}")
        return value


class AnswerQuestionRequest(RequestModel):
    """Alex's answer to something Gary asked."""

    message_id: EntityId
    answer: str = Field(min_length=1, max_length=1000)
