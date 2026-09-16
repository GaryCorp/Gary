import datetime as dt
import json
from typing import TYPE_CHECKING

from gary.db import Database
from gary.db.repositories import Repositories
from gary.policy import APPROVAL_EXPIRY_HOURS, SYSTEM_ACTOR, USER_ACTOR
from gary.services.common import Clock, NotFoundError, clock_now, default_clock
from gary.timeutil import format_utc, to_datetime

if TYPE_CHECKING:
    from gary.services.action_service import ActionService

DECISIONS = ("approved", "rejected")


def expire_stale_approvals(repos: Repositories, now: str) -> list[str]:
    """Expire pending approvals older than APPROVAL_EXPIRY_HOURS and cancel
    their actions. Runs inside the caller's transaction."""
    cutoff = format_utc(to_datetime(now) - dt.timedelta(hours=APPROVAL_EXPIRY_HOURS))
    expired = []
    for approval in repos.approvals.list_pending_created_before(cutoff):
        if not repos.approvals.resolve(approval["id"], "expired", "Not answered in time", now):
            continue
        action = repos.actions.get_by_approval(approval["id"])
        if action:
            repos.actions.transition(action["id"], "awaiting_approval", "cancelled")
        repos.audit.write(
            SYSTEM_ACTOR,
            "approval_expired",
            f"Approval expired: {approval['summary']}",
            "approval",
            approval["id"],
            {"action_id": action["id"] if action else None},
            now=now,
        )
        expired.append(approval["id"])
    return expired


class ApprovalService:
    def __init__(
        self,
        db: Database,
        action_service: "ActionService",
        clock: Clock = default_clock,
    ):
        self.db = db
        self.action_service = action_service
        self.clock = clock

    def list_pending(self) -> list[dict]:
        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            expire_stale_approvals(repos, now)
            pending = repos.approvals.list_pending()
            for approval in pending:
                action = repos.actions.get_by_approval(approval["id"])
                approval["action_id"] = action["id"] if action else None
            return pending

    def list_recent_resolved(self, limit: int = 20) -> list[dict]:
        with self.db.read() as conn:
            repos = Repositories.bind(conn)
            resolved = repos.approvals.list_recent_resolved(limit)
            for approval in resolved:
                action = repos.actions.get_by_approval(approval["id"])
                approval["action_status"] = action["status"] if action else None
                approval["action_error"] = action["error_message"] if action else None
            return resolved

    def get(self, approval_id: str) -> dict:
        with self.db.read() as conn:
            approval = Repositories.bind(conn).approvals.get(approval_id)
        if approval is None:
            raise NotFoundError(f"No approval with id {approval_id}")
        return {**approval, "payload": json.loads(approval["payload_json"])}

    async def resolve(
        self,
        approval_id: str,
        decision: str,
        actor: str = USER_ACTOR,
        channel: str = "web",
        note: str | None = None,
    ) -> dict:
        """Record the user's decision, then run the action if approved."""
        if decision not in DECISIONS:
            raise ValueError(f"decision must be one of {DECISIONS}")

        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            expire_stale_approvals(repos, now)

            approval = repos.approvals.get(approval_id)
            if approval is None:
                raise NotFoundError(f"No approval with id {approval_id}")
            if not repos.approvals.resolve(approval_id, decision, note, now):
                raise ValueError(f"This approval is already {approval['status']}")

            action = repos.actions.get_by_approval(approval_id)
            if action is not None:
                repos.actions.transition(
                    action["id"],
                    "awaiting_approval",
                    "approved" if decision == "approved" else "rejected",
                )
            repos.audit.write(
                actor,
                f"approval_{decision}",
                f"{decision.capitalize()} Gary's request: {approval['summary']}",
                "approval",
                approval_id,
                {
                    "channel": channel,
                    "note": note,
                    "action_id": action["id"] if action else None,
                },
                now=now,
            )

        result = {
            "approval_id": approval_id,
            "decision": decision,
            "summary": approval["summary"],
        }
        if decision == "approved" and action is not None:
            result["execution"] = await self.action_service.execute(action["id"])
        return result
