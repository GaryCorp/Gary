"""The facts a performance review stands on.

Deliberately free of any model. Everything here is arithmetic over what the
company already recorded, so a review can cite a number and anyone can check
it. What the numbers *mean* is the reviewer's job, and is stored separately.

The measure worth understanding is calibration: a specialist who reports
confidence 0.9 and fails is a different problem from one who reports 0.5 and
delivers. Counting only successes would miss it entirely.
"""

import datetime as dt
import json
from statistics import mean

from gary.db.repositories import Repositories
from gary.timeutil import format_utc, to_datetime

# Enough of a record to say anything at all. Below this a review would be
# opinion dressed as assessment.
MIN_ASSIGNMENTS = 1


def _just_after(now: str) -> str:
    return format_utc(to_datetime(now) + dt.timedelta(seconds=1))


def _minutes(started: str | None, finished: str | None) -> float | None:
    if not (started and finished):
        return None
    return round((to_datetime(finished) - to_datetime(started)).total_seconds() / 60, 1)


def _round(values: list[float], places: int = 2) -> float | None:
    return round(mean(values), places) if values else None


def employee_scorecard(repos: Repositories, agent_id: str, since: str, now: str) -> dict:
    """One specialist's record: what they were asked, and what came back."""
    assignments = [
        a
        for a in repos.assignments.list_recent(agent_id, None, 100)
        if (a["created_at"] or "") >= since
    ]
    completed = [a for a in assignments if a["status"] == "completed"]
    failed = [a for a in assignments if a["status"] == "failed"]

    runs, attempts, tool_calls, costs, durations = [], [], [], [], []
    for assignment in assignments:
        for run in repos.agent_runs.list_for_assignment(assignment["id"]):
            runs.append(run)
            attempts.append(run["attempts"] or 0)
            tool_calls.append(run["tool_calls"] or 0)
            if run["cost_usd"] is not None:
                costs.append(run["cost_usd"])
            minutes = _minutes(run["started_at"], run["completed_at"])
            if minutes is not None:
                durations.append(minutes)

    confidences, confident_failures = [], 0
    for assignment in assignments:
        try:
            result = json.loads(assignment["result_json"] or "{}")
        except json.JSONDecodeError:
            continue
        stated = result.get("confidence")
        if isinstance(stated, (int, float)):
            confidences.append(float(stated))
            if assignment["status"] != "completed" and stated >= 0.7:
                confident_failures += 1

    denials = repos.audit.count_events_for("agent_tool_denied", agent_id, since)
    # A report the runner refused to store: the work came back, but not in a
    # shape the company could use.
    rejected = repos.audit.count_events_for("agent_output_rejected", agent_id, since)

    return {
        "subject": agent_id,
        "period_start": since,
        "period_end": now,
        "assignments": len(assignments),
        "completed": len(completed),
        "failed": len(failed),
        "completion_rate": (
            round(len(completed) / len(assignments), 2) if assignments else None
        ),
        "mean_attempts": _round(attempts),
        "mean_tool_calls": _round(tool_calls),
        "mean_minutes_to_deliver": _round(durations, 1),
        "total_cost_usd": round(sum(costs), 4) if costs else 0.0,
        "cost_per_completed_report_usd": (
            round(sum(costs) / len(completed), 4) if costs and completed else None
        ),
        "mean_stated_confidence": _round(confidences),
        # Confident and wrong is worth more than either number alone.
        "confident_failures": confident_failures,
        "tool_denials": denials,
        "reports_rejected": rejected,
        "enough_to_review": len(assignments) >= MIN_ASSIGNMENTS,
    }


def principal_scorecard(repos: Repositories, since: str, now: str) -> dict:
    """Alex's own record. The company is only as unblocked as he is."""
    # list_completed_between is [start, end), so the end has to be just past
    # now or work finished in this very second falls outside its own review.
    completed = repos.tasks.list_completed_between(since, _just_after(now))
    overdue = repos.tasks.list_overdue(now)
    missed = repos.tasks.list_missed_blocks(now)

    resolved = [
        a for a in repos.approvals.list_recent_resolved(100)
        if (a["resolved_at"] or "") >= since
    ]
    answered_approvals = [a for a in resolved if a["status"] in ("approved", "rejected")]
    expired_approvals = [a for a in resolved if a["status"] == "expired"]

    # What Gary assigned him each morning, and how much of it was done that
    # day: the check-in records both, so following through is measurable.
    assigned = done_same_day = days_checked = 0
    for event in repos.audit.list_since("daily_checkin_asked", since):
        details = json.loads(event["details_json"] or "{}")
        assigned += details.get("assigned", 0)
        done_same_day += details.get("done", 0)
        days_checked += 1

    messages = [m for m in repos.spoken.list_recent(since, 100) if m["expects_reply"]]
    answered_questions = [m for m in messages if m["status"] == "answered"]
    ignored_questions = [m for m in messages if m["status"] == "expired"]

    tickets = repos.engineering.list_all(100)
    opened = [t for t in tickets if (t["created_at"] or "") >= since]
    done = [t for t in tickets if t["status"] == "done" and (t["updated_at"] or "") >= since]
    blocked = [t for t in tickets if t["status"] == "blocked"]

    return {
        "subject": "alex",
        "period_start": since,
        "period_end": now,
        "tasks_completed": len(completed),
        "tasks_overdue": len(overdue),
        "scheduled_blocks_missed": len(missed),
        "approvals_answered": len(answered_approvals),
        "approvals_expired_unanswered": len(expired_approvals),
        "questions_answered": len(answered_questions),
        "questions_left_unanswered": len(ignored_questions),
        "days_checked_in": days_checked,
        "tasks_assigned": assigned,
        "tasks_done_same_day": done_same_day,
        "engineering_opened": len(opened),
        "engineering_done": len(done),
        "engineering_blocked": len(blocked),
        "enough_to_review": bool(
            completed or overdue or missed or resolved or messages or tickets
        ),
    }


def manager_scorecard(repos: Repositories, since: str, now: str) -> dict:
    """Gary's record, as the people who work for him would see it."""
    runs = [r for r in repos.planning_runs.list_recent(100) if (r["started_at"] or "") >= since]
    assignments = [
        a for a in repos.assignments.list_recent(None, None, 100)
        if (a["created_at"] or "") >= since
    ]
    actions = repos.actions.list_recent(since, 200)
    succeeded = [a for a in actions if a["status"] == "succeeded"]
    failed = [a for a in actions if a["status"] == "failed"]

    raised = [m for m in repos.spoken.list_recent(since, 100) if m["expects_reply"]]
    answered = [m for m in raised if m["status"] == "answered"]

    spend = repos.usage.totals_since(since)

    return {
        "subject": "gary",
        "period_start": since,
        "period_end": now,
        "planning_cycles": len(runs),
        "delegations": len(assignments),
        "reports_received": sum(1 for a in assignments if a["status"] == "completed"),
        "actions_taken": len(actions),
        "actions_succeeded": len(succeeded),
        "actions_failed": len(failed),
        "things_raised_with_alex": len(raised),
        "answered_by_alex": len(answered),
        "ai_spend_usd": round(spend["cost_usd"] or 0, 4),
        "spend_per_cycle_usd": (
            round((spend["cost_usd"] or 0) / len(runs), 4) if runs else None
        ),
        "enough_to_review": bool(runs or assignments or actions),
    }


__all__ = [
    "MIN_ASSIGNMENTS",
    "employee_scorecard",
    "manager_scorecard",
    "principal_scorecard",
]
