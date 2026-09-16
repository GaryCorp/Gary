import datetime as dt
import json
from typing import Callable

from gary.db.repositories import Repositories
from gary.timeutil import format_utc, utc_now

Clock = Callable[[], dt.datetime]


class NotFoundError(ValueError):
    pass


def clock_now(clock: Clock) -> str:
    return format_utc(clock())


def default_clock() -> dt.datetime:
    return utc_now()


def metadata_json(metadata: dict | None) -> str | None:
    return None if metadata is None else json.dumps(metadata, sort_keys=True)


def require_project(repos: Repositories, project_id: str | None) -> dict | None:
    if project_id is None:
        return None
    project = repos.projects.get(project_id)
    if project is None:
        raise NotFoundError(f"No project with id {project_id}")
    return project


def require_task(repos: Repositories, task_id: str | None) -> dict | None:
    if task_id is None:
        return None
    task = repos.tasks.get(task_id)
    if task is None:
        raise NotFoundError(f"No task with id {task_id}")
    return task
