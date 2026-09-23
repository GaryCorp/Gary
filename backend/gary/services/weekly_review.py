"""What the company did while nobody was managing it.

A week of running itself produces a lot of small decisions and a few things
that quietly broke. This assembles the whole week from SQLite and renders it,
deterministically and with **no model call** -- which matters, because the
week you most want a report is the week the spend ceiling stopped everything
else. It is also why it can be trusted as a record: it reports what the
database says happened, not what a model remembers.

Nothing here judges. It states what was done, what it cost, what was decided,
and what is still stuck, and leaves the judging to Alex.
"""

import datetime as dt

from gary.db import Database
from gary.db.repositories import Repositories
from gary.services.common import Clock, clock_now, default_clock
from gary.services.planning_service import company_health
from gary.timeutil import format_utc, to_datetime, to_local

REVIEW_TITLE_PREFIX = "Weekly review "
# How much of any one list reaches the note. A week can produce a lot.
LIST_LIMIT = 12


def review_title(day: dt.date) -> str:
    return f"{REVIEW_TITLE_PREFIX}{day.isoformat()}"


class WeeklyReview:
    def __init__(
        self,
        db: Database,
        timezone,
        usage=None,
        clock: Clock = default_clock,
        accountability=None,
    ):
        self.db = db
        # How Alex did against what Gary assigned him, when Gary manages him.
        self.accountability = accountability
        self.timezone = timezone
        # The usage ledger, when the deployment has one: money is reported as
        # unmeasured rather than zero when it does not.
        self.usage = usage
        self.clock = clock

    def _local(self, value: str | None) -> str:
        return to_local(value, self.timezone)[:16].replace("T", " ") if value else ""

    def collect(self, days: int = 7) -> dict:
        """Everything the week contains, as data."""
        now = clock_now(self.clock)
        start = format_utc(to_datetime(now) - dt.timedelta(days=days))
        # list_completed_between is [start, end), so the end has to be just
        # past now or work finished in this very second is left out of its
        # own week.
        end = format_utc(to_datetime(now) + dt.timedelta(seconds=1))

        with self.db.read() as conn:
            repos = Repositories.bind(conn)

            completed = repos.tasks.list_completed_between(start, end)
            assignments = [
                a for a in repos.assignments.list_recent(None, None, 100)
                if (a["created_at"] or "") >= start
            ]
            tickets = [
                t for t in repos.engineering.list_all(100)
                if (t["created_at"] or "") >= start or (t["updated_at"] or "") >= start
            ]
            approvals = [
                a for a in repos.approvals.list_recent_resolved(100)
                if (a["resolved_at"] or "") >= start
            ]
            questions = [
                m for m in repos.spoken.list_recent(start, 100) if m["expects_reply"]
            ]
            purchases = repos.finance.list_purchases(start, 50)
            health = company_health(repos, now)

        return {
            "from": start,
            "to": now,
            "days": days,
            "completed_tasks": completed,
            "assignments": assignments,
            "engineering": tickets,
            "approvals": approvals,
            "questions": questions,
            "purchases": purchases,
            "spend": self._spend(days),
            "health": health,
            "you": self.accountability.week_summary(days) if self.accountability else None,
        }

    def _spend(self, days: int) -> dict:
        """What the week cost, or an honest statement that it is unknown."""
        if self.usage is None:
            return {"measured": False, "detail": "No usage ledger on this deployment."}
        summary = self.usage.summary(days)
        totals = summary["total"]
        unpriced = summary.get("unpriced_models") or []
        return {
            # A single unpriced call makes the total a floor, not a figure, so
            # it is reported as unmeasured rather than as a number.
            "measured": not unpriced,
            "cost_usd": round(totals.get("cost_usd") or 0, 4),
            "total_tokens": totals.get("total_tokens") or 0,
            "calls": totals.get("calls") or 0,
            "by_source": summary.get("by_source", [])[:LIST_LIMIT],
            "unpriced_models": unpriced,
        }

    # --------------------------------------------------------------- render

    def render_markdown(self, data: dict) -> str:
        lines = [f"# Week to {self._local(data['to'])[:10]}", ""]

        def section(name: str, items: list[str], empty: str) -> None:
            lines.append(f"## {name}")
            lines.extend(items[:LIST_LIMIT] or [empty])
            lines.append("")

        you = data.get("you")
        if you and you["days_checked_in"]:
            section(
                "Your week",
                [
                    f"- {you['done_same_day']} of {you['assigned']} assigned tasks done the "
                    f"day they were assigned, over {you['days_checked_in']} "
                    f"day{'s' if you['days_checked_in'] != 1 else ''}",
                    f"- {you['overdue_now']} task{'s' if you['overdue_now'] != 1 else ''} "
                    "overdue now",
                ],
                "",
            )

        section(
            "Work completed",
            [f"- {t['title']}" for t in data["completed_tasks"]],
            "- Nothing was marked complete this week.",
        )

        done = [a for a in data["assignments"] if a["status"] == "completed"]
        failed = [a for a in data["assignments"] if a["status"] == "failed"]
        section(
            "The team",
            [f"- {a['assigned_to']}: {a['objective'][:120]}" for a in done]
            + [f"- **{a['assigned_to']} did not finish**: {a['objective'][:120]}" for a in failed],
            "- Nobody was given an assignment this week.",
        )

        section(
            "Engineering",
            [
                f"- #{t['github_issue_number']} {t['priority']} {t['status']}"
                + (f" ({t['sync_state']})" if t["sync_state"] != "synced" else "")
                for t in data["engineering"]
            ],
            "- No engineering tickets moved this week.",
        )

        section(
            "Decisions",
            [f"- {a['status']}: {a['summary'][:120]}" for a in data["approvals"]]
            + [
                f"- asked: {m['text'][:100]}"
                + (f" -> {m['answer'][:60]}" if m["answer"] else " (no answer)")
                for m in data["questions"]
            ],
            "- Nothing needed a decision this week.",
        )

        section("Money", self._money_lines(data), "- Nothing was spent.")

        health = data["health"]
        if health["healthy"]:
            problems = ["- Nothing is stuck."]
        else:
            problems = [
                f"- {count} {name.replace('_', ' ')}"
                for name, count in health["counts"].items()
                if count
            ]
        section("What needs you", problems, "- Nothing is stuck.")

        return "\n".join(lines).rstrip() + "\n"

    def _money_lines(self, data: dict) -> list[str]:
        spend = data["spend"]
        lines = []
        if not spend.get("measured"):
            if spend.get("unpriced_models"):
                names = ", ".join(m["model"] for m in spend["unpriced_models"][:3])
                lines.append(
                    f"- AI spend is **unpriced** for {names}: "
                    f"{spend.get('total_tokens', 0):,} tokens counted, no cost claimed."
                )
            else:
                lines.append(f"- AI spend: {spend.get('detail', 'not measured')}")
        else:
            lines.append(
                f"- AI spend: ${spend['cost_usd']:.2f} over {spend['calls']} calls."
            )
            lines.extend(
                f"  - {row['source']}: ${row.get('cost_usd', 0):.2f}"
                for row in spend.get("by_source", [])
            )
        for purchase in data["purchases"][:LIST_LIMIT]:
            lines.append(
                f"- Card purchase {purchase.get('status', '?')}: "
                f"{purchase.get('merchant', '')} {purchase.get('amount_cents', 0) / 100:.2f}"
            )
        return lines

    def spoken_summary(self, data: dict) -> str:
        """The short version, for Piper. Plain words, no markdown."""
        done = len(data["completed_tasks"])
        reports = sum(1 for a in data["assignments"] if a["status"] == "completed")
        tickets = len(data["engineering"])
        stuck = sum(data["health"]["counts"].values())

        parts = [
            f"Here is the week. {done} task{'s' if done != 1 else ''} completed, "
            f"{reports} department report{'s' if reports != 1 else ''} back, "
            f"{tickets} engineering ticket{'s' if tickets != 1 else ''} touched."
        ]
        you = data.get("you")
        if you and you["days_checked_in"]:
            parts.append(
                f"You finished {you['done_same_day']} of the {you['assigned']} things I "
                f"gave you on the day I gave them, and {you['overdue_now']} "
                f"{'is' if you['overdue_now'] == 1 else 'are'} overdue now."
            )
        spend = data["spend"]
        if spend.get("measured"):
            parts.append(f"The company spent {spend['cost_usd']:.2f} dollars on AI.")
        elif spend.get("unpriced_models"):
            parts.append(
                "I cannot tell you what it cost: the model we are using has no price set."
            )
        if stuck:
            parts.append(
                f"{stuck} thing{'s' if stuck != 1 else ''} need you. "
                "The details are in the weekly review note."
            )
        else:
            parts.append("Nothing is stuck.")
        return " ".join(parts)


__all__ = ["WeeklyReview", "review_title", "REVIEW_TITLE_PREFIX"]
