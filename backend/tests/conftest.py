import asyncio
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gary import build_gary  # noqa: E402
from gary.models.action import ScheduleTaskPayload, SendExternalEmailPayload  # noqa: E402
from gary.models.common import RequestModel  # noqa: E402
from gary.models.project import CreateProjectRequest  # noqa: E402
from gary.models.task import CreateTaskRequest  # noqa: E402
from gary.services.action_service import ActionHandler  # noqa: E402
from gary.services.common import require_task  # noqa: E402

START = dt.datetime(2026, 9, 16, 14, 0, tzinfo=dt.timezone.utc)


class FakeClock:
    def __init__(self, now: dt.datetime = START):
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += dt.timedelta(**delta)


class FakeExternal:
    """Stands in for Google Calendar and Gmail."""

    def __init__(self):
        self.sent_emails = []
        self.events = {}
        self.fail_next = None

    async def send_email(self, payload: SendExternalEmailPayload, context: dict) -> dict:
        if self.fail_next:
            error, self.fail_next = self.fail_next, None
            raise RuntimeError(error)
        self.sent_emails.append(payload.to)
        return {"sent_message_id": f"msg-{len(self.sent_emails)}"}

    async def create_event(self, payload: ScheduleTaskPayload, context: dict) -> dict:
        if self.fail_next:
            error, self.fail_next = self.fail_next, None
            raise RuntimeError(error)
        event_id = f"event-{len(self.events) + 1}"
        self.events[event_id] = (payload.start, payload.end)
        return {"event_id": event_id}


def _check_schedule(repos, payload):
    task = require_task(repos, payload.task_id)
    if task["calendar_event_id"]:
        raise ValueError("already scheduled")
    return {"task": task}


def _record_schedule(repos, payload, result, now):
    repos.tasks.update(
        payload.task_id,
        now=now,
        status="scheduled",
        scheduled_start=payload.start,
        scheduled_end=payload.end,
        calendar_event_id=result["event_id"],
    )
    return {}


def fake_handlers(external: FakeExternal) -> dict[str, ActionHandler]:
    return {
        "send_external_email": ActionHandler(
            payload_model=SendExternalEmailPayload,
            summarize=lambda p, c: f"Email {p.to}: {p.subject}",
            execute=external.send_email,
        ),
        "schedule_task": ActionHandler(
            payload_model=ScheduleTaskPayload,
            summarize=lambda p, c: f"Schedule {c['task']['title']}",
            check=_check_schedule,
            execute=external.create_event,
            record=_record_schedule,
        ),
    }


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def external():
    return FakeExternal()


@pytest.fixture
def db_path(tmp_path):
    # A file, not :memory:, because every unit of work opens its own
    # connection and WAL behavior is under test.
    return tmp_path / "gary.db"


@pytest.fixture
def gary(db_path, clock, external):
    return build_gary(
        db_path,
        "America/Chicago",
        action_handlers=fake_handlers(external),
        clock=clock,
    )


def run(coroutine):
    return asyncio.run(coroutine)


def iso(value: dt.datetime) -> str:
    return value.isoformat()


def make_project(gary, **overrides) -> dict:
    data = {"name": "Chief of Staff video", "objective": "Publish the video"}
    data.update(overrides)
    return gary.projects.create_project(CreateProjectRequest(**data))


def make_task(gary, **overrides) -> dict:
    data = {"title": "Film demo"}
    data.update(overrides)
    return gary.tasks.create_task(CreateTaskRequest(**data))


def audit_events(gary, entity_type: str, entity_id: str) -> list[str]:
    with gary.db.read() as conn:
        rows = conn.execute(
            "SELECT event_type FROM audit_log WHERE entity_type = ? AND entity_id = ? ORDER BY id",
            (entity_type, entity_id),
        ).fetchall()
    return [row["event_type"] for row in rows]


__all__ = ["RequestModel"]
