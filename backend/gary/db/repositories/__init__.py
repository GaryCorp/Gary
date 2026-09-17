import sqlite3
from dataclasses import dataclass

from gary.db.repositories.actions import ActionRepository
from gary.db.repositories.agents import (
    AgentRepository,
    AgentRunRepository,
    AssignmentRepository,
    ManagementReviewRepository,
)
from gary.db.repositories.approvals import ApprovalRepository
from gary.db.repositories.audit import AuditRepository
from gary.db.repositories.commitments import CommitmentRepository
from gary.db.repositories.dependencies import DependencyRepository
from gary.db.repositories.followups import FollowupRepository
from gary.db.repositories.planning_runs import PlanningRunRepository
from gary.db.repositories.projects import ProjectRepository
from gary.db.repositories.tasks import TaskRepository


@dataclass
class Repositories:
    """All repositories bound to one connection, so a service can change
    several tables inside a single transaction."""

    projects: ProjectRepository
    tasks: TaskRepository
    dependencies: DependencyRepository
    followups: FollowupRepository
    commitments: CommitmentRepository
    approvals: ApprovalRepository
    actions: ActionRepository
    audit: AuditRepository
    planning_runs: PlanningRunRepository
    agents: AgentRepository
    assignments: AssignmentRepository
    reviews: ManagementReviewRepository
    agent_runs: AgentRunRepository

    @classmethod
    def bind(cls, conn: sqlite3.Connection) -> "Repositories":
        return cls(
            projects=ProjectRepository(conn),
            tasks=TaskRepository(conn),
            dependencies=DependencyRepository(conn),
            followups=FollowupRepository(conn),
            commitments=CommitmentRepository(conn),
            approvals=ApprovalRepository(conn),
            actions=ActionRepository(conn),
            audit=AuditRepository(conn),
            planning_runs=PlanningRunRepository(conn),
            agents=AgentRepository(conn),
            assignments=AssignmentRepository(conn),
            reviews=ManagementReviewRepository(conn),
            agent_runs=AgentRunRepository(conn),
        )


__all__ = [
    "ActionRepository",
    "AgentRepository",
    "AgentRunRepository",
    "AssignmentRepository",
    "ManagementReviewRepository",
    "ApprovalRepository",
    "AuditRepository",
    "CommitmentRepository",
    "DependencyRepository",
    "FollowupRepository",
    "PlanningRunRepository",
    "ProjectRepository",
    "Repositories",
    "TaskRepository",
]
