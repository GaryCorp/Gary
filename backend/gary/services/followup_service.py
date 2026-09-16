from gary.db import Database
from gary.db.repositories import Repositories
from gary.models.commitment import CreateCommitmentRequest, ResolveCommitmentRequest
from gary.models.followup import CompleteFollowupRequest, CreateFollowupRequest
from gary.policy import GARY_ACTOR
from gary.services.common import (
    Clock,
    NotFoundError,
    clock_now,
    default_clock,
    require_project,
    require_task,
)


class FollowupService:
    def __init__(self, db: Database, clock: Clock = default_clock):
        self.db = db
        self.clock = clock

    def create_followup(self, request: CreateFollowupRequest, actor: str = GARY_ACTOR) -> dict:
        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            require_project(repos, request.project_id)
            task = require_task(repos, request.task_id)
            project_id = request.project_id or (task["project_id"] if task else None)

            followup = repos.followups.create(
                title=request.title,
                due_at=request.due_at,
                description=request.description,
                priority=request.priority,
                project_id=project_id,
                task_id=request.task_id,
                now=now,
            )
            repos.audit.write(
                actor,
                "followup_created",
                f"Created follow-up: {followup['title']}",
                "followup",
                followup["id"],
                {"due_at": followup["due_at"], "task_id": followup["task_id"]},
                now=now,
            )
        return followup

    def complete_followup(
        self, request: CompleteFollowupRequest, actor: str = GARY_ACTOR
    ) -> dict:
        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            followup = repos.followups.get(request.followup_id)
            if followup is None:
                raise NotFoundError(f"No follow-up with id {request.followup_id}")
            if followup["status"] != "pending":
                raise ValueError(f"Follow-up is already {followup['status']}")

            followup = repos.followups.set_status(followup["id"], request.status, now)
            repos.audit.write(
                actor,
                f"followup_{request.status}",
                f"Follow-up {request.status}: {followup['title']}",
                "followup",
                followup["id"],
                now=now,
            )
        return followup

    def list_due(self) -> list[dict]:
        now = clock_now(self.clock)
        with self.db.read() as conn:
            return Repositories.bind(conn).followups.list_due(now)


class CommitmentService:
    def __init__(self, db: Database, clock: Clock = default_clock):
        self.db = db
        self.clock = clock

    def create_commitment(
        self, request: CreateCommitmentRequest, actor: str = GARY_ACTOR
    ) -> dict:
        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            require_project(repos, request.project_id)
            task = require_task(repos, request.task_id)
            project_id = request.project_id or (task["project_id"] if task else None)

            commitment = repos.commitments.create(
                description=request.description,
                committed_to=request.committed_to,
                deadline=request.deadline,
                project_id=project_id,
                task_id=request.task_id,
                now=now,
            )
            repos.audit.write(
                actor,
                "commitment_created",
                f"Recorded commitment: {commitment['description']}",
                "commitment",
                commitment["id"],
                {
                    "committed_to": commitment["committed_to"],
                    "deadline": commitment["deadline"],
                    "task_id": commitment["task_id"],
                },
                now=now,
            )
        return commitment

    def resolve_commitment(
        self, request: ResolveCommitmentRequest, actor: str = GARY_ACTOR
    ) -> dict:
        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            commitment = repos.commitments.get(request.commitment_id)
            if commitment is None:
                raise NotFoundError(f"No commitment with id {request.commitment_id}")
            if commitment["status"] != "open":
                raise ValueError(f"Commitment is already {commitment['status']}")

            commitment = repos.commitments.set_status(commitment["id"], request.status, now)
            repos.audit.write(
                actor,
                f"commitment_{request.status}",
                f"Commitment {request.status}: {commitment['description']}",
                "commitment",
                commitment["id"],
                now=now,
            )
        return commitment

    def list_open(self) -> list[dict]:
        with self.db.read() as conn:
            return Repositories.bind(conn).commitments.list_open()
