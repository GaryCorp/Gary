import datetime as dt
import json
import re
from typing import Callable

from gary.db.repositories import Repositories
from gary.services.readiness import task_readiness
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


def task_readiness_in(repos: Repositories, task: dict, now: str) -> dict:
    """task_readiness with its dependencies and project status read from the
    database, so every caller answers "is this ready" the same way."""
    project = repos.projects.get(task["project_id"]) if task["project_id"] else None
    return task_readiness(
        task,
        repos.dependencies.list_dependencies(task["id"]),
        now,
        project_status=project["status"] if project else None,
    )


def require_task(repos: Repositories, task_id: str | None) -> dict | None:
    if task_id is None:
        return None
    task = repos.tasks.get(task_id)
    if task is None:
        raise NotFoundError(f"No task with id {task_id}")
    return task


# --------------------------------------------------------- text similarity

# Punctuation carries no meaning about what a piece of work is about, and a
# trailing question mark would otherwise make "closed" and "closed?" different
# words -- which matters now that whole spoken sentences are compared.
PUNCTUATION = re.compile(r"[^\w\s]+")

# Words too common to say anything about what a piece of work is about.
OBJECTIVE_STOPWORDS = frozenset(
    "about with what which their there this that from into been have been would could "
    "should company gary garycorp report review research assess evaluate please".split()
)
# How much of the content words must match to count as the same thing.
REPEAT_OVERLAP = 0.6


def normalize_title(value: str) -> str:
    return " ".join(value.split()).casefold()


def objective_key(text: str) -> frozenset[str]:
    """Content words of an objective, for spotting work already commissioned."""
    words = {
        word
        for word in PUNCTUATION.sub(" ", normalize_title(text)).split()
        if len(word) > 3 and word not in OBJECTIVE_STOPWORDS
    }
    return frozenset(words)


def looks_like_repeat(new: frozenset[str], existing: list[frozenset[str]]) -> bool:
    """True when most of an objective's content words match earlier work."""
    if not new:
        return False
    for other in existing:
        if not other:
            continue
        if len(new & other) / len(new) >= REPEAT_OVERLAP:
            return True
    return False
