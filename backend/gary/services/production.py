"""The weekly video schedule: one episode published every week.

Episodes are filmed in batches: every ``episodes_per_shoot`` weeks one shoot
films that many episodes, and each is then edited, given a thumbnail and a
title, and published on its own publish day. With two per shoot, a shoot on
Saturday the 26th films the episodes published on Friday the 2nd and Friday
the 9th.

Everything here is arithmetic and ordinary rows. An episode is a project whose
tasks carry deadlines worked back from its publish time, so planning,
readiness, overdue alerts and the briefing need nothing special to follow it.
No model is called: the schedule is the same every week, so Python writes it,
and the planner only decides *when* in the week the flexible work happens.

Two kinds of work are fixed in time and are put on the calendar here rather
than left to the planner: the shoot, which is usually outside working hours,
and the publish slot. Both go through the ordinary schedule_task action, so
they are checked, recorded and audited like anything else Gary schedules.
"""

import datetime as dt
from dataclasses import dataclass
from typing import Callable
from zoneinfo import ZoneInfo

from gary.db import Database
from gary.db.repositories import Repositories
from gary.models.action import ProposeActionRequest
from gary.models.task import CreateTaskRequest
from gary.policy import SYSTEM_ACTOR
from gary.services.common import Clock, clock_now, default_clock
from gary.services.task_service import add_dependency_in, create_task_in
from gary.timeutil import format_utc, to_datetime

DAY_END = dt.time(17, 0)
# A stage's GitHub issue opens this long before its work can start, so the
# board shows the coming week and not the whole schedule.
TICKET_LOOKAHEAD = dt.timedelta(days=7)
# After a failed attempt to open an issue, wait this long before trying again.
TICKET_RETRY_AFTER = dt.timedelta(hours=6)

# Task titles are "<episode>: <stage>", and the shoot is "Film <episodes>".
STAGE_TITLES = {
    "script": "Script",
    "edit": "Edit",
    "thumbnail": "Thumbnail and title",
    "publish": "Publish",
}
FILM_PREFIX = "Film "

# What each stage's issue asks for. Plain and fixed: a stage is the same
# work every week, so nothing here needs a model to write it.
STAGE_TICKETS = {
    "script": {
        "priority": "P2",
        "objective": "Write the script for {episode} so it is ready to film by {deadline}.",
        "requirements": ["Outline the main points", "Write the full script"],
        "acceptance_criteria": ["The script is finished and ready to film"],
    },
    "film": {
        "priority": "P1",
        "objective": "Film {episode} in one session, starting {start}.",
        "requirements": [
            "Set up camera, sound and lighting",
            "Film each episode from its script",
        ],
        "acceptance_criteria": ["Footage for every episode is recorded and backed up"],
    },
    "edit": {
        "priority": "P1",
        "objective": "Edit {episode} so it is ready to publish, by {deadline}.",
        "requirements": ["Cut the footage to the script", "Add audio, captions and graphics"],
        "acceptance_criteria": ["A final export is ready to upload"],
    },
    "thumbnail": {
        "priority": "P2",
        "objective": "Make the thumbnail and write the title for {episode} by {deadline}.",
        "requirements": ["Design the thumbnail", "Write the title and description"],
        "acceptance_criteria": ["Thumbnail, title and description are ready to upload"],
    },
    "publish": {
        "priority": "P1",
        "objective": "Upload and publish {episode} at {deadline}.",
        "requirements": [
            "Upload the final export",
            "Add the title, description and thumbnail",
            "Publish",
        ],
        "acceptance_criteria": ["The video is live"],
    },
}


def stage_of(title: str) -> tuple[str, str] | None:
    """(stage, episode) for a schedule task title, or None."""
    if title.startswith(FILM_PREFIX):
        return "film", title[len(FILM_PREFIX):]
    episode, _, stage = title.rpartition(": ")
    for key, name in STAGE_TITLES.items():
        if stage == name and episode:
            return key, episode
    return None


@dataclass(frozen=True)
class ProductionSchedule:
    first_shoot: dt.date
    series: str = "Video"
    first_episode: int = 1
    episodes_per_shoot: int = 2
    publish_weekday: int = 4  # Friday; Monday is 0
    publish_time: dt.time = dt.time(17, 0)
    shoot_time: dt.time = dt.time(10, 0)
    # How far ahead a batch is planned, counted to its shoot day. Two weeks
    # gives the scripts a full week before the shoot they are needed for.
    horizon_days: int = 14
    script_minutes: int = 120
    film_minutes_per_episode: int = 120
    edit_minutes: int = 240
    thumbnail_minutes: int = 60
    publish_minutes: int = 30

    def __post_init__(self):
        if self.first_episode < 1:
            raise ValueError("The first episode number must be 1 or more")
        if not 1 <= self.episodes_per_shoot <= 8:
            raise ValueError("Episodes per shoot must be between 1 and 8")
        if not 0 <= self.publish_weekday <= 6:
            raise ValueError("The publish day must be a weekday number, 0 to 6")

    def batch_of(self, episode: int) -> int:
        return (episode - self.first_episode) // self.episodes_per_shoot

    def batch_episodes(self, batch: int) -> list[int]:
        first = self.first_episode + batch * self.episodes_per_shoot
        return list(range(first, first + self.episodes_per_shoot))

    def shoot_date(self, batch: int) -> dt.date:
        # A batch covers episodes_per_shoot weekly slots, so the next shoot
        # is that many weeks later and the cadence stays one a week.
        return self.first_shoot + dt.timedelta(weeks=batch * self.episodes_per_shoot)

    def publish_date(self, episode: int) -> dt.date:
        """The first publish day after the episode's shoot, plus one week per
        episode ahead of it in the batch."""
        batch = self.batch_of(episode)
        shoot = self.shoot_date(batch)
        days_ahead = (self.publish_weekday - shoot.weekday()) % 7 or 7
        position = (episode - self.first_episode) % self.episodes_per_shoot
        return shoot + dt.timedelta(days=days_ahead, weeks=position)


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def schedule_from_settings(
    first_shoot: str,
    series: str = "Video",
    first_episode: str = "1",
    episodes_per_shoot: str = "2",
    publish_day: str = "fri",
    publish_time: str = "17:00",
    shoot_time: str = "10:00",
) -> ProductionSchedule | None:
    """The schedule from .env strings, or None when PRODUCTION_FIRST_SHOOT
    is empty, which turns the weekly schedule off."""
    if not first_shoot.strip():
        return None

    def parse(name: str, value: str, convert):
        try:
            return convert(value.strip())
        except ValueError:
            raise ValueError(f"{name} is not valid: {value!r}") from None

    day = publish_day.strip().lower()[:3]
    if day not in WEEKDAYS:
        raise ValueError(f"PRODUCTION_PUBLISH_DAY must be a weekday such as fri, not {publish_day!r}")
    return ProductionSchedule(
        first_shoot=parse("PRODUCTION_FIRST_SHOOT (YYYY-MM-DD)", first_shoot, dt.date.fromisoformat),
        series=" ".join(series.split()) or "Video",
        first_episode=parse("PRODUCTION_FIRST_EPISODE", first_episode, int),
        episodes_per_shoot=parse("PRODUCTION_EPISODES_PER_SHOOT", episodes_per_shoot, int),
        publish_weekday=WEEKDAYS.index(day),
        publish_time=parse("PRODUCTION_PUBLISH_TIME (HH:MM)", publish_time, dt.time.fromisoformat),
        shoot_time=parse("PRODUCTION_SHOOT_TIME (HH:MM)", shoot_time, dt.time.fromisoformat),
    )


def _at(day: dt.date, time: dt.time, zone: ZoneInfo) -> str:
    return format_utc(dt.datetime.combine(day, time, tzinfo=zone))


def _monday(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def episode_label(schedule: ProductionSchedule, episode: int) -> str:
    return f"{schedule.series} {episode}"


def batch_plan(schedule: ProductionSchedule, batch: int, zone: ZoneInfo) -> dict:
    """Every task of one shoot and its episodes, with local-time deadlines
    worked back from each publish slot. Pure: no database, easy to check."""
    episodes = schedule.batch_episodes(batch)
    shoot_day = schedule.shoot_date(batch)
    shoot_start = dt.datetime.combine(shoot_day, schedule.shoot_time, tzinfo=zone)
    shoot_end = shoot_start + dt.timedelta(
        minutes=schedule.film_minutes_per_episode * len(episodes)
    )
    labels = [episode_label(schedule, n) for n in episodes]
    film = {
        "key": "film",
        "title": FILM_PREFIX + " and ".join(labels),
        "priority": 8,
        "estimated_minutes": schedule.film_minutes_per_episode * len(episodes),
        "earliest_start": format_utc(shoot_start),
        "deadline": format_utc(shoot_end),
    }

    plans = []
    for episode, label in zip(episodes, labels):
        publish_day = schedule.publish_date(episode)
        publish_at = dt.datetime.combine(publish_day, schedule.publish_time, tzinfo=zone)
        # Post-production starts in the publish week, or after the shoot if
        # that is later, so a batch's second episode waits its turn.
        post_start = max(
            shoot_end, dt.datetime.combine(_monday(publish_day), dt.time(0, 0), tzinfo=zone)
        )
        tasks = [
            {
                "key": "script",
                "title": f"{label}: {STAGE_TITLES['script']}",
                "priority": 6,
                "estimated_minutes": schedule.script_minutes,
                "earliest_start": _at(_monday(shoot_day), dt.time(0, 0), zone),
                "deadline": _at(shoot_day - dt.timedelta(days=1), DAY_END, zone),
            },
            {
                "key": "edit",
                "title": f"{label}: {STAGE_TITLES['edit']}",
                "priority": 7,
                "estimated_minutes": schedule.edit_minutes,
                "earliest_start": format_utc(post_start),
                "deadline": _at(publish_day - dt.timedelta(days=2), DAY_END, zone),
            },
            {
                "key": "thumbnail",
                "title": f"{label}: {STAGE_TITLES['thumbnail']}",
                "priority": 6,
                "estimated_minutes": schedule.thumbnail_minutes,
                "earliest_start": format_utc(post_start),
                "deadline": _at(publish_day - dt.timedelta(days=1), DAY_END, zone),
            },
            {
                "key": "publish",
                "title": f"{label}: {STAGE_TITLES['publish']}",
                "priority": 8,
                "estimated_minutes": schedule.publish_minutes,
                "earliest_start": format_utc(
                    publish_at - dt.timedelta(minutes=schedule.publish_minutes)
                ),
                "deadline": format_utc(publish_at),
            },
        ]
        plans.append(
            {
                "episode": episode,
                "name": label,
                "objective": f"Publish {label} on {publish_day.strftime('%A %B %-d')}.",
                "publish_at": format_utc(publish_at),
                "tasks": tasks,
            }
        )
    return {
        "batch": batch,
        "shoot_at": format_utc(shoot_start),
        "film": film,
        "episodes": plans,
    }


def ticket_payload(task: dict, zone: ZoneInfo) -> dict | None:
    """The create_engineering_ticket payload for one schedule task, or None
    when the task is not a schedule stage."""
    found = stage_of(task["title"])
    if found is None:
        return None
    stage, episode = found
    spec = STAGE_TICKETS[stage]

    def when(value: str) -> str:
        return to_datetime(value).astimezone(zone).strftime("%A %B %-d at %-I:%M %p")

    return {
        "task_id": task["id"],
        "title": task["title"],
        "objective": spec["objective"].format(
            episode=episode, deadline=when(task["deadline"]), start=when(task["earliest_start"])
        ),
        "requirements": spec["requirements"],
        "acceptance_criteria": spec["acceptance_criteria"],
        "priority": spec["priority"],
        "kind": "production",
        "estimated_minutes": task["estimated_minutes"],
        "due_at": task["deadline"],
    }


class ProductionService:
    def __init__(
        self,
        db: Database,
        actions,
        schedule: ProductionSchedule,
        timezone: ZoneInfo,
        clock: Clock = default_clock,
        engineering: Callable[[], object | None] = lambda: None,
    ):
        self.db = db
        self.actions = actions
        self.schedule = schedule
        self.timezone = timezone
        self.clock = clock
        # The GitHub ticket service, or None when GitHub is not configured;
        # the schedule works without it, it just has no issues.
        self.engineering = engineering

    async def run(self) -> dict:
        """Plan every batch now within the horizon, then put the fixed-time
        work on the calendar. Safe to repeat: nothing is created twice."""
        created = self.plan_upcoming()
        scheduled, failed = await self.schedule_fixed_tasks()
        return {"created": created, "scheduled": scheduled, "failed": failed}

    def plan_upcoming(self) -> list[int]:
        """Create the batches whose shoot is within the horizon. Returns the
        episode numbers created. A batch whose shoot day has already passed
        is never created: planning work into the past only makes overdue
        noise, and the numbering carries on after it."""
        now = clock_now(self.clock)
        today = to_datetime(now).astimezone(self.timezone).date()
        created = []
        batch = 0
        while (shoot := self.schedule.shoot_date(batch)) <= today + dt.timedelta(
            days=self.schedule.horizon_days
        ):
            if shoot >= today:
                created += self._create_batch(batch, now)
            batch += 1
        return created

    def _create_batch(self, batch: int, now: str) -> list[int]:
        plan = batch_plan(self.schedule, batch, self.timezone)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            if any(repos.production.get(e["episode"]) for e in plan["episodes"]):
                return []

            projects, scripts, post = [], [], []
            for episode in plan["episodes"]:
                project = repos.projects.create(
                    name=episode["name"],
                    objective=episode["objective"],
                    status="active",
                    priority=7,
                    deadline=episode["publish_at"],
                    metadata={"production_episode": episode["episode"]},
                    now=now,
                )
                repos.audit.write(
                    SYSTEM_ACTOR,
                    "project_created",
                    f"Created project: {project['name']}",
                    "project",
                    project["id"],
                    {"priority": project["priority"], "deadline": project["deadline"],
                     "tasks": len(episode["tasks"])},
                    now=now,
                )
                tasks = {
                    item["key"]: create_task_in(
                        repos,
                        CreateTaskRequest(
                            project_id=project["id"],
                            title=item["title"],
                            priority=item["priority"],
                            estimated_minutes=item["estimated_minutes"],
                            earliest_start=item["earliest_start"],
                            deadline=item["deadline"],
                        ),
                        now,
                        SYSTEM_ACTOR,
                    )
                    for item in episode["tasks"]
                }
                projects.append(project)
                scripts.append(tasks["script"])
                post.append(tasks)

            # The shoot belongs to the batch's first episode and every episode
            # in the batch waits on it.
            spec = plan["film"]
            film = create_task_in(
                repos,
                CreateTaskRequest(
                    project_id=projects[0]["id"],
                    title=spec["title"],
                    priority=spec["priority"],
                    estimated_minutes=spec["estimated_minutes"],
                    earliest_start=spec["earliest_start"],
                    deadline=spec["deadline"],
                ),
                now,
                SYSTEM_ACTOR,
            )
            for script in scripts:
                add_dependency_in(repos, film, script, now, SYSTEM_ACTOR)
            for tasks in post:
                add_dependency_in(repos, tasks["edit"], film, now, SYSTEM_ACTOR)
                add_dependency_in(repos, tasks["thumbnail"], film, now, SYSTEM_ACTOR)
                add_dependency_in(repos, tasks["publish"], tasks["edit"], now, SYSTEM_ACTOR)
                add_dependency_in(repos, tasks["publish"], tasks["thumbnail"], now, SYSTEM_ACTOR)

            for episode, project, tasks in zip(plan["episodes"], projects, post):
                repos.production.create(
                    episode_number=episode["episode"],
                    project_id=project["id"],
                    film_task_id=film["id"],
                    publish_task_id=tasks["publish"]["id"],
                    shoot_at=plan["shoot_at"],
                    publish_at=episode["publish_at"],
                    now=now,
                )
                repos.audit.write(
                    SYSTEM_ACTOR,
                    "production_episode_planned",
                    f"Planned {episode['name']} for {episode['publish_at']}",
                    "project",
                    project["id"],
                    {"episode": episode["episode"], "shoot_at": plan["shoot_at"],
                     "publish_at": episode["publish_at"], "film_task_id": film["id"]},
                    now=now,
                )
        return [episode["episode"] for episode in plan["episodes"]]

    async def schedule_fixed_tasks(self) -> tuple[list[str], list[dict]]:
        """Put upcoming shoots and publish slots on the calendar at their
        fixed times. The shoot is usually on a weekend, which is outside
        working hours by design, so the override is explicit and limited to
        these two kinds of task."""
        now = clock_now(self.clock)
        with self.db.read() as conn:
            pending = Repositories.bind(conn).production.list_unscheduled_fixed_tasks(now)

        scheduled, failed = [], []
        for task in pending:
            result = await self.actions.propose(
                ProposeActionRequest(
                    action_type="schedule_task",
                    payload={
                        "task_id": task["id"],
                        "start": task["earliest_start"],
                        "end": task["deadline"],
                        "override_working_hours": True,
                    },
                    reason="Fixed time in the weekly video schedule",
                    task_id=task["id"],
                ),
                actor=SYSTEM_ACTOR,
            )
            if result.get("status") == "succeeded":
                scheduled.append(task["title"])
            else:
                failed.append({"task": task["title"], "error": result.get("error")})
        return scheduled, failed

    async def sync_tickets(self) -> dict:
        """Give Alex this week's stages as GitHub issues, and close the ones
        he has finished in Gary. Each issue is opened through the ordinary
        create_engineering_ticket action; one task has at most one issue."""
        result = {"opened": [], "closed": [], "failed": []}
        service = self.engineering()
        if service is None:
            return result

        now = clock_now(self.clock)
        cutoff = format_utc(to_datetime(now) + TICKET_LOOKAHEAD)
        retry_since = format_utc(to_datetime(now) - TICKET_RETRY_AFTER)
        with self.db.read() as conn:
            repos = Repositories.bind(conn)
            wanted = [
                task for task in repos.production.list_tasks_needing_ticket(cutoff)
                if not repos.actions.failed_since(
                    "create_engineering_ticket", task["id"], retry_since
                )
            ]
            finished = repos.production.list_open_tickets_for_finished_tasks()

        for task in wanted:
            payload = ticket_payload(task, self.timezone)
            if payload is None:
                continue
            outcome = await self.actions.propose(
                ProposeActionRequest(
                    action_type="create_engineering_ticket",
                    payload=payload,
                    reason="This week's work in the video schedule",
                    task_id=task["id"],
                ),
                actor=SYSTEM_ACTOR,
            )
            if outcome.get("status") == "succeeded":
                result["opened"].append(task["title"])
            else:
                result["failed"].append({"task": task["title"], "error": outcome.get("error")})

        for row in finished:
            try:
                await service.close_production_ticket(row, actor=SYSTEM_ACTOR)
                result["closed"].append(row["github_issue_number"])
            except Exception as exc:  # GitHub down: the next tick tries again
                result["failed"].append(
                    {"issue": row["github_issue_number"], "error": str(exc)[:300]}
                )
        return result

    def status(self) -> list[dict]:
        """Every episode with where its work stands, for the status page."""
        with self.db.read() as conn:
            repos = Repositories.bind(conn)
            episodes = repos.production.list_all()
            for episode in episodes:
                episode["tasks"] = [
                    {"title": t["title"], "status": t["status"], "deadline": t["deadline"],
                     "scheduled_start": t["scheduled_start"]}
                    for t in repos.tasks.list_for_project(episode["project_id"])
                ]
        return episodes
