"""Engineering tickets: Gary's bridge to Alex's GitHub workflow.

    Gary task (SQLite, authoritative company state)
        <-> engineering_tickets row (the link, one per task)
            <-> private GitHub issue (Alex's specification)
                <-> private Project item (workflow status)

SQLite stays the source of truth for company planning; GitHub is the source of
truth for the issue's external workflow state, so synchronization adopts
GitHub's Status and issue state.

Order of work follows the existing external side-effect rule: validate and
reserve state in a short transaction, call GitHub with no transaction open,
then record the outcome. A GitHub failure is recorded as a failure, never as
success, and the ticket row is written before the issue is created so a retry
can never open a second issue for the same task.
"""

import logging

from gary.container import Gary
from gary.db.repositories import Repositories
from gary.integrations.github.client import GitHubClient
from gary.integrations.github.exceptions import (
    GitHubError,
    GitHubNotFoundError,
    GitHubPrivacyError,
)
from gary.integrations.github.issues import (
    ensure_labels,
    render_issue_body,
    render_title,
    scrub,
    ticket_labels,
)
from gary.integrations.github.models import (
    ALLOWED_TRANSITIONS,
    EngineeringStatus,
)
from gary.integrations.github.privacy import PrivacyGate
from gary.integrations.github.projects import PRIORITY_FIELD, ProjectBoard
from gary.models.engineering import (
    CreateEngineeringTicketRequest,
    EngineeringTicket,
)
from gary.models.task import CompleteTaskRequest, UpdateTaskRequest
from gary.policy import GARY_ACTOR, USER_ACTOR
from gary.services.common import Clock, NotFoundError, clock_now, default_clock
from gary.services.readiness import CLOSED_STATUSES
from gary.timeutil import format_utc, to_local

logger = logging.getLogger("gary.engineering")

GITHUB_ACTOR = "github"
# Gary task status while engineering work is in each ticket state.
TASK_STATUS_FOR = {
    EngineeringStatus.IN_PROGRESS: "in_progress",
    EngineeringStatus.BLOCKED: "blocked",
}


class EngineeringError(RuntimeError):
    """The request cannot be carried out as asked (wrong state, no ticket)."""


class EngineeringTicketService:
    def __init__(
        self,
        gary: Gary,
        client: GitHubClient,
        board: ProjectBoard | None = None,
        gate: PrivacyGate | None = None,
        clock: Clock = default_clock,
    ):
        self.gary = gary
        self.client = client
        self.config = client.config
        self.board = board or ProjectBoard(client, cache=_FieldCache(gary))
        self.gate = gate or PrivacyGate(client)
        self.clock = clock

    # ------------------------------------------------------------- utilities

    def _now(self) -> str:
        return clock_now(self.clock)

    def _audit(self, actor: str, event: str, summary: str, ticket_id: str, details: dict) -> None:
        with self.gary.db.transaction() as conn:
            Repositories.bind(conn).audit.write(
                actor, event, summary, "engineering_ticket", ticket_id, details, now=self._now()
            )

    def _row(self, ticket_id: str) -> dict:
        with self.gary.db.read() as conn:
            row = Repositories.bind(conn).engineering.get(ticket_id)
        if row is None:
            raise NotFoundError(f"No engineering ticket with id {ticket_id}")
        return row

    def _save(self, ticket_id: str, **changes) -> dict:
        with self.gary.db.transaction() as conn:
            return Repositories.bind(conn).engineering.update(ticket_id, now=self._now(), **changes)

    def _present(self, row: dict) -> EngineeringTicket:
        with self.gary.db.read() as conn:
            task = Repositories.bind(conn).tasks.get(row["task_id"])
        return EngineeringTicket.from_row(row, task)

    def resolve(
        self, ticket_id: str | None = None, task_id: str | None = None, issue_number: int | None = None
    ) -> dict:
        with self.gary.db.read() as conn:
            repos = Repositories.bind(conn)
            row = None
            if ticket_id:
                row = repos.engineering.get(ticket_id)
            elif task_id:
                row = repos.engineering.get_for_task(task_id)
            elif issue_number:
                row = repos.engineering.get_by_issue(
                    self.config.owner, self.config.repository, issue_number
                )
            else:
                raise EngineeringError("Give ticket_id, task_id, or issue_number")
        if row is None:
            raise NotFoundError("No engineering ticket matches that id")
        return row

    # ---------------------------------------------------------------- health

    async def status(self) -> dict:
        """Health of the integration, for Gary and the status endpoint."""
        report = await self.gate.audit()
        if report.safe_to_operate:
            state, detail = "healthy", report.detail
        elif report.repository_private is False or report.project_private is False:
            state, detail = "privacy_error", report.detail
        else:
            state, detail = _state_from_error(report.detail), report.detail
        return {
            "state": state,
            "detail": detail,
            "repository": self.config.full_name,
            "project_number": self.config.project_number,
            "engineer": self.config.engineer_username,
            **report.as_dict(),
        }

    async def audit_github_privacy(self) -> dict:
        return (await self.gate.audit()).as_dict()

    # ------------------------------------------------------------- creation

    async def create_ticket(
        self, request: CreateEngineeringTicketRequest, actor: str = GARY_ACTOR
    ) -> EngineeringTicket:
        """Create (or finish creating) the engineering ticket for a task.

        Idempotent: the row is reserved first, so calling twice for the same
        task continues the existing ticket instead of opening a second issue.
        """
        now = self._now()
        with self.gary.db.transaction() as conn:
            repos = Repositories.bind(conn)
            task = repos.tasks.get(request.task_id)
            if task is None:
                raise NotFoundError(f"No task with id {request.task_id}")
            if task["status"] in CLOSED_STATUSES:
                raise EngineeringError(
                    f"Task {task['title']!r} is {task['status']}; it needs an open task"
                )
            existing = repos.engineering.get_for_task(request.task_id)
            if existing and existing["sync_state"] == "synced":
                raise EngineeringError(
                    f"Task {task['title']!r} already has engineering ticket "
                    f"#{existing['github_issue_number']}; update it instead of creating another"
                )
            row = existing or repos.engineering.create(
                task_id=request.task_id,
                owner=self.config.owner,
                repository=self.config.repository,
                assigned_to=self.config.engineer_username,
                priority=request.priority,
                security_review_required=request.security_review_required,
                now=now,
            )
            project = repos.projects.get(task["project_id"]) if task["project_id"] else None

        # Fails closed: a public repository or Project stops us here, before
        # anything company-related reaches GitHub.
        try:
            repository, board_project = await self.gate.verify()
        except GitHubPrivacyError as exc:
            self._save(row["id"], sync_state="pending", sync_error=str(exc)[:500])
            self._audit(
                actor,
                "github_privacy_check_failed",
                "Refused to create a GitHub issue: privacy check failed",
                row["id"],
                {"task_id": request.task_id, "error": str(exc)[:500]},
            )
            raise

        if not repository.has_issues:
            raise EngineeringError(
                f"Issues are disabled on {repository.full_name}; enable them in GitHub first"
            )

        labels = ticket_labels(
            request.priority, request.kind, request.security_review_required
        )
        row = await self._ensure_issue(row, request, task, project, labels, actor)
        row = await self._ensure_assignment(row, actor)
        row = await self._ensure_project_item(row, board_project.id, actor)
        row = await self._ensure_status(row, board_project.id, EngineeringStatus.READY, actor)
        await self._set_optional_fields(row, board_project.id, request, actor)

        if row["sync_state"] == "pending":
            row = self._save(row["id"], sync_state="synced", sync_error=None, last_synced_at=self._now())
        return self._present(row)

    async def _ensure_issue(self, row, request, task, project, labels, actor) -> dict:
        if row["github_issue_number"]:
            return row
        try:
            await ensure_labels(self.client, labels)
        except GitHubError as exc:
            # Labels are not worth failing the ticket over.
            logger.warning("Could not ensure labels: %s", exc)

        body = render_issue_body(
            objective=request.objective,
            requirements=request.requirements,
            acceptance_criteria=request.acceptance_criteria,
            priority=request.priority,
            task_id=request.task_id,
            project_name=(project or {}).get("name"),
            security_requirements=request.security_requirements,
            dependencies=request.dependencies,
            estimated_minutes=request.estimated_minutes or task.get("estimated_minutes"),
            due_at_local=to_local(request.due_at or task.get("deadline"), self.gary.timezone),
        )
        issue = await self.client.create_issue(
            title=render_title(request.title),
            body=body,
            labels=labels,
            assignees=[self.config.engineer_username],
        )
        row = self._save(
            row["id"],
            github_issue_number=issue.number,
            github_issue_node_id=issue.node_id,
            github_url=issue.html_url,
            assignment_confirmed=int(self.config.engineer_username in issue.assignees),
        )
        self._audit(
            actor,
            "github_issue_created",
            f"Created private issue #{issue.number}: {issue.title[:120]}",
            row["id"],
            {
                "task_id": row["task_id"],
                "repository": self.config.full_name,
                "issue_number": issue.number,
                "labels": labels,
            },
        )
        self._audit(
            actor,
            "engineering_ticket_created",
            f"Engineering ticket for {task['title'][:100]} (#{issue.number})",
            row["id"],
            {"task_id": row["task_id"], "priority": row["priority"],
             "security_review_required": bool(row["security_review_required"])},
        )
        if self.config.engineer_username in issue.assignees:
            self._audit(
                actor,
                "engineering_ticket_assigned",
                f"Issue #{issue.number} assigned to {self.config.engineer_username}",
                row["id"],
                {"assignee": self.config.engineer_username},
            )
        return row

    async def _ensure_assignment(self, row, actor) -> dict:
        if row["assignment_confirmed"] or not row["github_issue_number"]:
            return row
        username = self.config.engineer_username
        try:
            issue = await self.client.assign_issue(row["github_issue_number"], [username])
            confirmed = username in issue.assignees
        except GitHubError as exc:
            confirmed = False
            error = f"Could not assign {username}: {exc}"
        else:
            error = None if confirmed else (
                f"GitHub did not add {username} as an assignee. They may not have access "
                f"to {self.config.full_name}."
            )

        row = self._save(
            row["id"],
            assignment_confirmed=int(confirmed),
            sync_state="synced" if confirmed and row["sync_state"] == "synced" else (
                "degraded" if not confirmed else row["sync_state"]
            ),
            sync_error=None if confirmed else (error or "")[:500],
        )
        if confirmed:
            self._audit(
                actor,
                "engineering_ticket_assigned",
                f"Issue #{row['github_issue_number']} assigned to {username}",
                row["id"],
                {"assignee": username},
            )
        else:
            logger.warning("Assignment failed for ticket %s: %s", row["id"], error)
            self._audit(
                actor,
                "github_sync_failed",
                f"Could not assign {username} to issue #{row['github_issue_number']}",
                row["id"],
                {"error": (error or "")[:500], "step": "assignment"},
            )
        return row

    async def _ensure_project_item(self, row, project_id, actor) -> dict:
        if row["github_project_item_id"]:
            return row
        try:
            item_id = await self.board.add_issue(project_id, row["github_issue_node_id"])
        except GitHubError as exc:
            row = self._save(row["id"], sync_state="degraded", sync_error=str(exc)[:500])
            self._audit(
                actor,
                "github_sync_failed",
                f"Issue #{row['github_issue_number']} was not added to the Project",
                row["id"],
                {"error": str(exc)[:500], "step": "project_item"},
            )
            return row
        row = self._save(row["id"], github_project_id=project_id, github_project_item_id=item_id)
        self._audit(
            actor,
            "github_project_item_added",
            f"Issue #{row['github_issue_number']} added to the private Engineering Project",
            row["id"],
            {"project_number": self.config.project_number},
        )
        return row

    async def _ensure_status(self, row, project_id, status: EngineeringStatus, actor) -> dict:
        if not row["github_project_item_id"]:
            return row
        try:
            option = await self.board.set_status(project_id, row["github_project_item_id"], status)
        except GitHubError as exc:
            row = self._save(row["id"], sync_state="degraded", sync_error=str(exc)[:500])
            self._audit(
                actor,
                "github_sync_failed",
                f"Could not set Project Status to {status.project_option}",
                row["id"],
                {"error": str(exc)[:500], "step": "status"},
            )
            return row
        if option is None:
            # The board has no such option, so the ticket did not really move.
            message = (
                f"The Project has no {status.project_option!r} Status option, so the board "
                "was not changed. Add the option in GitHub, then reconcile this ticket."
            )
            row = self._save(row["id"], sync_state="degraded", sync_error=message[:500])
            self._audit(
                actor,
                "github_sync_failed",
                f"Project Status {status.project_option} is not available on the board",
                row["id"],
                {"step": "status", "error": message},
            )
            return row
        before = row["status"]
        row = self._save(row["id"], status=status.value, last_synced_at=self._now())
        self._audit(
            actor,
            "engineering_status_changed",
            f"Engineering ticket #{row['github_issue_number']} is now {status.project_option}",
            row["id"],
            {"from": before, "to": status.value, "project_status": option},
        )
        return row

    async def _set_optional_fields(self, row, project_id, request, actor) -> None:
        if not row["github_project_item_id"]:
            return
        try:
            await self.board.set_optional_fields(
                project_id,
                row["github_project_item_id"],
                priority=row["priority"],
                estimate_minutes=request.estimated_minutes,
                task_id=row["task_id"],
            )
        except GitHubError as exc:
            logger.warning("Optional Project fields not set for %s: %s", row["id"], exc)

    async def retry_incomplete(self, ticket_id: str, actor: str = GARY_ACTOR) -> EngineeringTicket:
        """Finish a ticket whose GitHub setup stopped part way."""
        row = self._row(ticket_id)
        if not row["github_issue_number"]:
            raise EngineeringError(
                "This ticket has no GitHub issue yet; create the ticket again for the same task"
            )
        _, project = await self.gate.verify()
        row = await self._ensure_assignment(row, actor)
        row = await self._ensure_project_item(row, project.id, actor)
        if row["github_project_item_id"] and row["sync_state"] != "synced":
            status = EngineeringStatus(row["status"])
            row = await self._ensure_status(
                row, project.id, EngineeringStatus.READY if status is EngineeringStatus.BACKLOG else status, actor
            )
        if row["github_project_item_id"] and row["assignment_confirmed"]:
            row = self._save(row["id"], sync_state="synced", sync_error=None, last_synced_at=self._now())
        return self._present(row)

    # ----------------------------------------------------------- transitions

    async def transition(
        self,
        row: dict,
        target: EngineeringStatus,
        reason: str | None = None,
        actor: str = GARY_ACTOR,
    ) -> EngineeringTicket:
        current = EngineeringStatus(row["status"])
        if current is target:
            return self._present(row)
        if target not in ALLOWED_TRANSITIONS[current]:
            raise EngineeringError(
                f"An engineering ticket cannot go from {current.project_option} to "
                f"{target.project_option}"
            )
        if (
            target is EngineeringStatus.DONE
            and row["security_review_required"]
            and current is not EngineeringStatus.SECURITY_REVIEW
        ):
            raise EngineeringError(
                "This ticket requires security review: it has to pass Security Review "
                "before it can be marked Done"
            )
        if not row["github_project_item_id"]:
            raise EngineeringError(
                "This ticket is not on the Engineering Project yet; reconcile it first"
            )

        _, project = await self.gate.verify()
        option = await self.board.set_status(project.id, row["github_project_item_id"], target)
        if option is None:
            raise EngineeringError(
                f"The Engineering Project has no {target.project_option!r} Status option, so "
                "the ticket was not moved. Add the option in GitHub first."
            )
        row = self._save(row["id"], status=target.value, last_synced_at=self._now())
        self._audit(
            actor,
            "engineering_ticket_blocked" if target is EngineeringStatus.BLOCKED
            else "engineering_status_changed",
            f"Issue #{row['github_issue_number']}: {current.project_option} -> {target.project_option}",
            row["id"],
            {"from": current.value, "to": target.value, "project_status": option,
             "reason": (reason or "")[:500]},
        )

        await self._mirror_task(row, target, reason, actor)
        if reason:
            await self._comment(row, f"{target.project_option}: {scrub(reason)}", actor, audit=False)
        if target is EngineeringStatus.BLOCKED:
            await self._label(row, add="blocked")
        elif current is EngineeringStatus.BLOCKED:
            await self._label(row, remove="blocked")
        if target is EngineeringStatus.DONE:
            if current is EngineeringStatus.SECURITY_REVIEW:
                row = self._save(row["id"], security_reviewed_at=self._now())
            row = await self._complete(row, actor)
        return self._present(row)

    async def set_priority(
        self,
        row: dict,
        priority: str,
        reason: str | None = None,
        actor: str = GARY_ACTOR,
    ) -> EngineeringTicket:
        """Re-prioritise a ticket everywhere it is recorded.

        The stored priority, the issue label and the board's Priority field
        have to agree, so GitHub is written first and SQLite only after it
        succeeded. A half-applied change is raised, never reported as done.
        """
        current = row["priority"]
        if current == priority:
            raise EngineeringError(
                f"Issue #{row['github_issue_number']} is already {priority}"
            )
        if not row["github_issue_number"]:
            raise EngineeringError("This ticket has no GitHub issue yet")

        _, project = await self.gate.verify()
        if row["github_project_item_id"]:
            written = await self.board.set_optional_fields(
                project.id, row["github_project_item_id"], priority=priority
            )
            if PRIORITY_FIELD not in written:
                raise EngineeringError(
                    f"The Engineering Project would not accept Priority {priority!r}, so "
                    "the ticket was not re-prioritised. Check the board's Priority options."
                )
        # The label follows the field. It is cosmetic, so a failure here is
        # logged rather than losing a priority GitHub already accepted.
        await self._label(row, add=priority, remove=current)

        row = self._save(row["id"], priority=priority, last_synced_at=self._now())
        self._audit(
            actor,
            "engineering_priority_changed",
            f"Issue #{row['github_issue_number']}: {current} -> {priority}",
            row["id"],
            {"from": current, "to": priority, "reason": (reason or "")[:500]},
        )
        note = f"Priority changed from {current} to {priority}."
        if reason:
            note += f" {scrub(reason)}"
        await self._comment(row, note, actor, audit=False)
        return self._present(row)

    async def _mirror_task(self, row, target: EngineeringStatus, reason, actor) -> None:
        status = TASK_STATUS_FOR.get(target)
        if status is None:
            return
        try:
            self.gary.tasks.update_task(
                UpdateTaskRequest(task_id=row["task_id"], status=status), actor
            )
        except (ValueError, NotFoundError) as exc:
            logger.warning("Could not mirror ticket state onto task %s: %s", row["task_id"], exc)

    async def _complete(self, row, actor) -> dict:
        """Done: close the issue and complete the Gary task."""
        try:
            await self.client.update_issue(
                row["github_issue_number"], state="closed", state_reason="completed"
            )
        except GitHubError as exc:
            logger.warning("Could not close issue #%s: %s", row["github_issue_number"], exc)
            self._audit(
                actor,
                "github_sync_failed",
                f"Project Status is Done but issue #{row['github_issue_number']} did not close",
                row["id"],
                {"error": str(exc)[:500], "step": "close_issue"},
            )
        try:
            self.gary.tasks.complete_task(CompleteTaskRequest(task_id=row["task_id"]), actor)
        except (ValueError, NotFoundError) as exc:
            logger.warning("Could not complete task %s: %s", row["task_id"], exc)
        row = self._save(row["id"], sync_state="synced", last_synced_at=self._now())
        self._audit(
            actor,
            "engineering_ticket_completed",
            f"Engineering ticket #{row['github_issue_number']} is done",
            row["id"],
            {"task_id": row["task_id"], "security_review_required": bool(row["security_review_required"])},
        )
        return row

    async def _label(self, row, add: str | None = None, remove: str | None = None) -> None:
        try:
            if add:
                await self.client.add_labels(row["github_issue_number"], [add])
            if remove:
                await self.client.remove_label(row["github_issue_number"], remove)
        except GitHubError as exc:
            logger.warning("Label change failed on #%s: %s", row["github_issue_number"], exc)

    # ------------------------------------------------------------- comments

    async def add_comment(self, row: dict, comment: str, actor: str = GARY_ACTOR) -> dict:
        return await self._comment(row, comment, actor)

    async def _comment(self, row, comment: str, actor: str, audit: bool = True) -> dict:
        if not row["github_issue_number"]:
            raise EngineeringError("This ticket has no GitHub issue to comment on")
        await self.gate.verify()
        body = scrub(comment.strip())
        posted = await self.client.add_issue_comment(row["github_issue_number"], body)
        if audit:
            self._audit(
                actor,
                "engineering_ticket_commented",
                f"Commented on issue #{row['github_issue_number']}",
                row["id"],
                {"length": len(body)},
            )
        return {"issue_number": row["github_issue_number"], "comment_url": posted.get("html_url", "")}

    # --------------------------------------------------------------- reading

    def get_ticket(self, **lookup) -> EngineeringTicket:
        return self._present(self.resolve(**lookup))

    def list_tickets(self, status: str | None = None, open_only: bool = True, limit: int = 10):
        with self.gary.db.read() as conn:
            repos = Repositories.bind(conn)
            if status:
                rows = repos.engineering.list_by_status((status,), limit)
            elif open_only:
                rows = repos.engineering.list_open(limit)
            else:
                rows = repos.engineering.list_all(limit)
            tasks = {t["id"]: t for t in repos.tasks.get_many([r["task_id"] for r in rows])}
        return [EngineeringTicket.from_row(row, tasks.get(row["task_id"])) for row in rows]

    # ----------------------------------------------------- synchronization

    async def sync_ticket(self, ticket_id: str, actor: str = GITHUB_ACTOR) -> dict:
        """Bring SQLite in line with GitHub for one ticket.

        GitHub owns the workflow state, so its Project Status and issue state
        win. Anything that does not add up (a closed issue that is not Done, a
        Done ticket that still needs security review) is flagged for
        reconciliation instead of silently completing company work.
        """
        row = self._row(ticket_id)
        if not row["github_issue_number"]:
            return {"ticket_id": ticket_id, "synced": False, "reason": "no GitHub issue yet"}
        item_lost = False
        try:
            await self.gate.verify()
            issue = await self.client.get_issue(row["github_issue_number"])
        except GitHubError as exc:
            self._save(row["id"], sync_state="degraded", sync_error=str(exc)[:500])
            self._audit(
                actor,
                "github_sync_failed",
                f"Could not synchronize issue #{row['github_issue_number']}",
                row["id"],
                {"error": str(exc)[:500]},
            )
            return {"ticket_id": ticket_id, "synced": False, "error": str(exc)}

        # The Project item is read on its own, because a deleted item must not
        # stop the issue syncing. Its id is remembered in SQLite, so an item
        # removed from the board leaves a reference that will never resolve
        # again; clearing it lets retry_incomplete put the issue back on the
        # board instead of the ticket being stuck degraded forever.
        board_status = None
        if row["github_project_item_id"]:
            try:
                board_status = await self.board.status_of(row["github_project_item_id"])
            except GitHubNotFoundError:
                item_lost = True
            except GitHubError as exc:
                self._save(row["id"], sync_state="degraded", sync_error=str(exc)[:500])
                self._audit(
                    actor,
                    "github_sync_failed",
                    f"Could not read the Project item for issue #{row['github_issue_number']}",
                    row["id"],
                    {"error": str(exc)[:500], "step": "project_item"},
                )
                return {"ticket_id": ticket_id, "synced": False, "error": str(exc)}

        changes: dict = {"last_synced_at": self._now()}
        notes: list[str] = []
        before = EngineeringStatus(row["status"])

        if item_lost:
            changes["github_project_item_id"] = None
            changes["sync_state"] = "degraded"
            changes["sync_error"] = (
                f"Issue #{row['github_issue_number']} is no longer on the Project board; "
                "it will be added again on the next retry"
            )
            notes.append("Project item is gone; it will be re-added")
            self._audit(
                actor,
                "github_project_item_lost",
                f"Issue #{row['github_issue_number']} is no longer on the private Project",
                row["id"],
                {"previous_item_id": row["github_project_item_id"]},
            )

        confirmed = self.config.engineer_username in issue.assignees
        if int(confirmed) != row["assignment_confirmed"]:
            changes["assignment_confirmed"] = int(confirmed)
            notes.append("assignee changed on GitHub")

        status = before
        if board_status is not None and board_status is not before:
            status = board_status
            changes["status"] = board_status.value
            notes.append(f"Project Status is {board_status.project_option}")

        needs_review = bool(row["security_review_required"])
        blocked_label = "blocked" in [label.casefold() for label in issue.labels]
        if blocked_label and status not in (EngineeringStatus.BLOCKED, EngineeringStatus.DONE):
            notes.append("issue carries the blocked label")

        # Reaching Done from Security Review is what satisfies the review
        # requirement. Nobody here approves on Dave's behalf.
        reviewed_at = row["security_reviewed_at"]
        if status is EngineeringStatus.DONE and before is EngineeringStatus.SECURITY_REVIEW:
            reviewed_at = changes["security_reviewed_at"] = self._now()

        reconcile = False
        if issue.state == "closed":
            satisfied = status is EngineeringStatus.DONE and (not needs_review or bool(reviewed_at))
            if satisfied:
                changes["sync_state"] = "synced"
            else:
                reconcile = True
                changes["sync_state"] = "needs_reconciliation"
                changes["sync_error"] = (
                    f"Issue #{issue.number} is closed but the ticket is "
                    f"{status.project_option}"
                    + (" and has not passed security review" if needs_review and not reviewed_at else "")
                )
                notes.append("closed without satisfying the required state")
        elif item_lost:
            # Already marked degraded above; it is not synced until re-added.
            pass
        elif changes.get("status") or row["sync_state"] != "synced":
            changes["sync_state"] = "synced"
            changes.setdefault("sync_error", None)

        row = self._save(row["id"], **changes)

        if "status" in changes:
            self._audit(
                actor,
                "engineering_status_changed",
                f"GitHub moved #{issue.number} to {status.project_option}",
                row["id"],
                {"from": before.value, "to": status.value, "source": "github"},
            )
            await self._mirror_task(row, status, None, actor)

        if issue.state == "closed" and not reconcile and row["status"] == EngineeringStatus.DONE.value:
            with self.gary.db.read() as conn:
                task = Repositories.bind(conn).tasks.get(row["task_id"])
            if task and task["status"] not in CLOSED_STATUSES:
                try:
                    self.gary.tasks.complete_task(CompleteTaskRequest(task_id=row["task_id"]), actor)
                except (ValueError, NotFoundError) as exc:
                    logger.warning("Could not complete task %s: %s", row["task_id"], exc)
                self._audit(
                    actor,
                    "engineering_ticket_completed",
                    f"Engineering ticket #{issue.number} completed on GitHub",
                    row["id"],
                    {"task_id": row["task_id"]},
                )
        if reconcile:
            self._audit(
                actor,
                "github_sync_failed",
                f"Issue #{issue.number} needs reconciliation",
                row["id"],
                {"reason": changes.get("sync_error", "")},
            )

        return {
            "ticket_id": row["id"],
            "synced": True,
            "issue_number": issue.number,
            "issue_state": issue.state,
            "status": row["status"],
            "assignment_confirmed": bool(row["assignment_confirmed"]),
            "needs_reconciliation": reconcile,
            "item_lost": item_lost,
            "notes": notes,
        }

    async def sync_all(self, limit: int = 100, actor: str = GITHUB_ACTOR) -> dict:
        with self.gary.db.read() as conn:
            rows = Repositories.bind(conn).engineering.list_syncable(limit)
        results, repaired = [], []
        for row in rows:
            try:
                result = await self.sync_ticket(row["id"], actor)
            except (GitHubError, NotFoundError) as exc:
                logger.warning("Sync failed for %s: %s", row["id"], exc)
                results.append({"ticket_id": row["id"], "synced": False, "error": str(exc)})
                continue
            results.append(result)
            # An issue that fell off the board goes straight back on, through
            # the same path that puts it there at creation.
            if result.get("item_lost"):
                try:
                    await self.retry_incomplete(row["id"], actor)
                    repaired.append(row["id"])
                except (GitHubError, NotFoundError, EngineeringError) as exc:
                    logger.warning("Could not put %s back on the Project: %s", row["id"], exc)
        return {
            "checked": len(results),
            "synced": sum(1 for r in results if r.get("synced")),
            "repaired": repaired,
            "needs_reconciliation": [r["ticket_id"] for r in results if r.get("needs_reconciliation")],
            "failed": [r["ticket_id"] for r in results if not r.get("synced")],
            "results": results,
        }


class _FieldCache:
    """Stores discovered Project field ids in SQLite (non-secret)."""

    def __init__(self, gary: Gary):
        self._gary = gary

    def load(self, project_id: str) -> dict | None:
        try:
            with self._gary.db.read() as conn:
                return Repositories.bind(conn).project_fields.load(project_id)
        except Exception:  # a cache miss must never break the integration
            logger.debug("Project field cache unavailable", exc_info=True)
            return None

    def save(self, project_id: str, fields: dict) -> None:
        try:
            with self._gary.db.transaction() as conn:
                Repositories.bind(conn).project_fields.save(
                    project_id, fields, now=format_utc(self._gary.planning.clock())
                )
        except Exception:
            logger.debug("Could not cache Project fields", exc_info=True)


def _state_from_error(detail: str) -> str:
    lowered = (detail or "").casefold()
    if "credential" in lowered and "rejected" in lowered:
        return "authentication_error"
    if "permission" in lowered:
        return "permission_error"
    if "not found" in lowered or "does not match" in lowered:
        return "misconfigured"
    return "unavailable"


__all__ = ["EngineeringError", "EngineeringTicketService", "GITHUB_ACTOR", "USER_ACTOR"]
