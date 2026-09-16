from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from gary.db import Database, apply_migrations
from gary.services.action_service import ActionHandler, ActionService
from gary.services.approval_service import ApprovalService
from gary.services.briefing import BriefingService
from gary.services.calendar_blocks import WorkWeek
from gary.services.common import Clock, default_clock
from gary.services.followup_service import CommitmentService, FollowupService
from gary.services.internal_actions import internal_action_handlers
from gary.services.planning_service import PlanningService
from gary.services.project_service import ProjectService
from gary.services.task_service import TaskService


@dataclass
class Gary:
    db: Database
    timezone: ZoneInfo
    projects: ProjectService
    tasks: TaskService
    followups: FollowupService
    commitments: CommitmentService
    planning: PlanningService
    actions: ActionService
    approvals: ApprovalService
    briefing: BriefingService
    week: WorkWeek


def build_gary(
    db_path: str | Path,
    timezone: str = "UTC",
    action_handlers: dict[str, ActionHandler] | None = None,
    clock: Clock = default_clock,
    migrate: bool = True,
    work_week: WorkWeek | None = None,
) -> Gary:
    db = Database(db_path)
    if migrate:
        apply_migrations(db)

    handlers = {**internal_action_handlers(), **(action_handlers or {})}
    actions = ActionService(db, handlers, clock)
    zone = ZoneInfo(timezone)
    week = work_week or WorkWeek()

    return Gary(
        db=db,
        timezone=zone,
        projects=ProjectService(db, clock),
        tasks=TaskService(db, clock),
        followups=FollowupService(db, clock),
        commitments=CommitmentService(db, clock),
        planning=PlanningService(db, clock),
        actions=actions,
        approvals=ApprovalService(db, actions, clock),
        briefing=BriefingService(db, zone, week, clock),
        week=week,
    )
