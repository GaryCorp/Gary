"""Action proposals and execution.

Flow for every action:

    Gary proposes a structured action
    -> the payload is validated against the action type's model
    -> policy (code, not Gary) sets the risk level
    -> green executes, yellow creates an approval, red is rejected
    -> the result is recorded and audited

Execution follows the external side-effect rule: validate, run the external
action with no transaction open, then record the outcome in a short
transaction. A failure is recorded as failed, never as success.
"""

import json
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from gary.db import Database
from gary.db.repositories import Repositories
from gary.models.action import ProposeActionRequest
from gary.models.common import RequestModel, validate_request
from gary.policy import GARY_ACTOR, GREEN, RED, escalate, policy_risk
from gary.services.common import (
    Clock,
    NotFoundError,
    clock_now,
    default_clock,
    require_project,
    require_task,
)

logger = logging.getLogger("gary.actions")


@dataclass(frozen=True)
class ActionHandler:
    payload_model: type[RequestModel]
    # Human-readable description, shown for approval.
    summarize: Callable[[RequestModel, dict], str]
    # Validates against current state inside a transaction and returns context
    # for execute (e.g. the task row). Runs at proposal and again at execution.
    check: Callable[[Repositories, RequestModel], dict] | None = None
    # May raise the policy risk level (never lower it).
    classify: Callable[[Repositories, RequestModel], str | None] | None = None
    # The external side effect. Runs with no transaction open.
    execute: Callable[[RequestModel, dict], Awaitable[dict]] | None = None
    # SQLite changes after the side effect succeeded, inside a transaction.
    record: Callable[[Repositories, RequestModel, dict, str], dict | None] | None = None
    # Extra audit event on success, e.g. calendar_changed or email_sent.
    audit_event: str | None = None


class ActionService:
    def __init__(
        self,
        db: Database,
        handlers: dict[str, ActionHandler],
        clock: Clock = default_clock,
        on_approval_requested: Callable[[dict], Awaitable[None]] | None = None,
    ):
        self.db = db
        self.handlers = handlers
        self.clock = clock
        # Told about every yellow action, after the approval is committed, so
        # the application can raise it with the user instead of leaving it to
        # be found on the approvals page. Never affects the proposal: an
        # approval that was recorded stands whether or not it was announced.
        self.on_approval_requested = on_approval_requested

    def supported_action_types(self) -> list[str]:
        return sorted(
            action_type
            for action_type in self.handlers
            if policy_risk(action_type) not in (None, RED)
        )

    async def propose(self, request: ProposeActionRequest, actor: str = GARY_ACTOR) -> dict:
        now = clock_now(self.clock)
        action_type = request.action_type
        base_risk = policy_risk(action_type)

        if base_risk == RED:
            with self.db.transaction() as conn:
                repos = Repositories.bind(conn)
                action = repos.actions.create(
                    action_type=action_type,
                    payload=request.payload,
                    risk_level=RED,
                    status="rejected",
                    reason=request.reason,
                    error_message="Rejected by policy",
                    now=now,
                )
                repos.audit.write(
                    actor,
                    "action_rejected_by_policy",
                    f"Rejected {action_type}: not allowed by policy",
                    "action",
                    action["id"],
                    {"reason": request.reason},
                    now=now,
                )
            return {
                "action_id": action["id"],
                "status": "rejected",
                "risk_level": RED,
                "message": f"{action_type} is not allowed by policy.",
            }

        handler = self.handlers.get(action_type)
        if base_risk is None or handler is None:
            raise ValueError(
                f"Unsupported action_type {action_type}. Supported: "
                f"{', '.join(self.supported_action_types())}"
            )

        payload = validate_request(handler.payload_model, request.payload)
        payload_data = payload.model_dump(mode="json", exclude_unset=True)
        task_id = request.task_id or getattr(payload, "task_id", None)

        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            require_project(repos, request.project_id)
            require_task(repos, task_id)
            context = handler.check(repos, payload) if handler.check else {}
            risk = escalate(
                base_risk, handler.classify(repos, payload) if handler.classify else None
            )
            summary = handler.summarize(payload, context)

            if risk == GREEN:
                action = repos.actions.create(
                    action_type=action_type,
                    payload=payload_data,
                    risk_level=risk,
                    status="approved",
                    reason=request.reason,
                    project_id=request.project_id,
                    task_id=task_id,
                    now=now,
                )
                repos.audit.write(
                    actor,
                    "action_proposed",
                    f"{summary} (green: runs automatically)",
                    "action",
                    action["id"],
                    {"action_type": action_type, "reason": request.reason},
                    now=now,
                )
            else:
                approval = repos.approvals.create(
                    action_type=action_type,
                    summary=summary,
                    payload=payload_data,
                    risk_level=risk,
                    reason=request.reason,
                    requested_by=actor,
                    now=now,
                )
                action = repos.actions.create(
                    action_type=action_type,
                    payload=payload_data,
                    risk_level=risk,
                    status="awaiting_approval",
                    reason=request.reason,
                    project_id=request.project_id,
                    task_id=task_id,
                    approval_id=approval["id"],
                    now=now,
                )
                repos.audit.write(
                    actor,
                    "approval_requested",
                    f"Requested approval: {summary}",
                    "approval",
                    approval["id"],
                    {
                        "action_id": action["id"],
                        "action_type": action_type,
                        "risk_level": risk,
                        "reason": request.reason,
                    },
                    now=now,
                )

        if risk == GREEN:
            return {**await self.execute(action["id"]), "risk_level": risk}

        await self._announce_approval(
            {
                "approval_id": approval["id"],
                "action_id": action["id"],
                "action_type": action_type,
                "summary": summary,
                "risk_level": risk,
                "reason": request.reason,
                # So the application can say something more speakable than the
                # page's summary, which is written to be read rather than heard.
                "payload": payload_data,
            }
        )

        return {
            "action_id": action["id"],
            "approval_id": approval["id"],
            "status": "awaiting_approval",
            "risk_level": risk,
            "summary": summary,
        }

    async def _announce_approval(self, approval: dict) -> None:
        if self.on_approval_requested is None:
            return
        try:
            await self.on_approval_requested(approval)
        except Exception:
            # Telling the user is best effort; the approval is already
            # recorded and the approvals page still has it.
            logger.exception(
                "Could not raise approval %s with the user", approval["approval_id"]
            )

    async def execute(self, action_id: str, actor: str = GARY_ACTOR) -> dict:
        """Run an approved action once."""
        now = clock_now(self.clock)

        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            action = repos.actions.get(action_id)
            if action is None:
                raise NotFoundError(f"No action with id {action_id}")
            if not repos.actions.transition(action_id, "approved", "executing"):
                raise ValueError(f"Action is {action['status']}, not approved")

            handler = self.handlers.get(action["action_type"])
            try:
                if handler is None:
                    raise ValueError(f"No handler for {action['action_type']}")
                payload = validate_request(
                    handler.payload_model, json.loads(action["payload_json"])
                )
                # State may have changed since the proposal was approved.
                context = handler.check(repos, payload) if handler.check else {}
            except ValueError as exc:
                self._mark_failed(repos, action, str(exc), None, now, actor)
                return self._failure(action, str(exc))

        result: dict = {}
        if handler.execute is not None:
            try:
                result = await handler.execute(payload, context) or {}
            except Exception as exc:
                logger.exception("Action %s (%s) failed", action_id, action["action_type"])
                with self.db.transaction() as conn:
                    self._mark_failed(
                        Repositories.bind(conn),
                        action,
                        str(exc) or type(exc).__name__,
                        None,
                        clock_now(self.clock),
                        actor,
                    )
                return self._failure(action, str(exc) or type(exc).__name__)

        now = clock_now(self.clock)
        try:
            with self.db.transaction() as conn:
                repos = Repositories.bind(conn)
                if handler.record is not None:
                    result = {**result, **(handler.record(repos, payload, result, now) or {})}
                repos.actions.update(
                    action_id,
                    status="succeeded",
                    executed_at=now,
                    result_json=json.dumps(result, sort_keys=True),
                )
                repos.audit.write(
                    actor,
                    "action_succeeded",
                    f"Done: {handler.summarize(payload, context)}",
                    "action",
                    action_id,
                    {"action_type": action["action_type"], "result": result},
                    now=now,
                )
                if handler.audit_event:
                    repos.audit.write(
                        actor,
                        handler.audit_event,
                        handler.summarize(payload, context),
                        "action",
                        action_id,
                        {"payload": payload.model_dump(mode="json"), "result": result},
                        now=now,
                    )
        except Exception as exc:
            # The external side effect already happened and cannot be rolled
            # back: record that honestly rather than pretending either way.
            logger.exception("Recording action %s failed", action_id)
            message = f"The action ran but recording its result failed: {exc}"
            with self.db.transaction() as conn:
                self._mark_failed(Repositories.bind(conn), action, message, result, now, actor)
            return self._failure(action, message)

        return {
            "action_id": action_id,
            "status": "succeeded",
            "summary": handler.summarize(payload, context),
            "result": result,
        }

    @staticmethod
    def _mark_failed(
        repos: Repositories,
        action: dict,
        error: str,
        result: dict | None,
        now: str,
        actor: str,
    ) -> None:
        repos.actions.update(
            action["id"],
            status="failed",
            executed_at=now,
            error_message=error,
            result_json=None if result is None else json.dumps(result, sort_keys=True),
        )
        repos.audit.write(
            actor,
            "action_failed",
            f"Failed {action['action_type']}: {error}",
            "action",
            action["id"],
            {"action_type": action["action_type"], "error": error},
            now=now,
        )

    @staticmethod
    def _failure(action: dict, error: str) -> dict:
        return {"action_id": action["id"], "status": "failed", "error": error}
