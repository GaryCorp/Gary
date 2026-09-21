"""What Gary says when he speaks first: the words only.

Deciding to speak and delivering it are SpokenDelivery's job; these build the
sentences, for Piper to read aloud, so no markdown, lists or symbols.
"""

from app.config import WAKE_WORD_DISPLAY
from app.gmail import single_line
from app.local_time import spoken_clock
from gary.policy import WEB_ONLY_APPROVAL_ACTIONS


def operations_announcement(alerts: dict) -> str | None:
    def names(items: list[dict], key: str) -> str:
        titles = [single_line(item[key])[:80] for item in items[:3]]
        extra = len(items) - len(titles)
        return ", ".join(titles) + (f", and {extra} more" if extra > 0 else "")

    parts = []
    for block in alerts.get("missed_blocks", [])[:2]:
        text = (
            f"{single_line(block['title'])[:80]} was scheduled until "
            f"{spoken_clock(block['scheduled_end'])} but is not marked done"
        )
        if block["affects"]:
            text += f", and it holds up {', '.join(block['affects'][:2])}"
        parts.append(text + ".")
    if alerts["due_followups"]:
        count = len(alerts["due_followups"])
        label = "A follow-up is" if count == 1 else f"{count} follow-ups are"
        parts.append(f"{label} due: {names(alerts['due_followups'], 'title')}.")
    if alerts["overdue_tasks"]:
        count = len(alerts["overdue_tasks"])
        label = "A task is" if count == 1 else f"{count} tasks are"
        parts.append(f"{label} now overdue: {names(alerts['overdue_tasks'], 'title')}.")
    if not parts:
        return None
    return " ".join(parts) + f" Say {WAKE_WORD_DISPLAY} to plan."


def spoken_summary(action_type: str, summary: str, payload: dict) -> str:
    """The approval, in words worth hearing.

    An approval summary is written to be read on the page, so it carries
    detail that is tedious out loud — a hire's full tool list, for one. The
    page keeps all of it; this is the version Alex hears.
    """
    if action_type == "hire_employee" and payload.get("name"):
        tools = len(payload.get("tools") or [])
        return (
            f"I would like to hire {payload['name']} as {payload.get('title', 'a new employee')}, "
            f"with {tools} read only tool{'s' if tools != 1 else ''}. "
            f"{single_line(str(payload.get('capability_gap', '')))[:200]}"
        )
    if action_type == "card_purchase" and payload.get("merchant"):
        dollars = (payload.get("amount_cents") or 0) / 100
        return (
            f"Catherine wants to spend {dollars:.2f} dollars at "
            f"{single_line(str(payload['merchant']))[:80]}. "
            f"{single_line(str(payload.get('description', '')))[:150]}"
        )
    return single_line(summary)[:300]


def approval_announcement(approval: dict) -> str:
    """What Gary says when something of his is waiting on Alex.

    Hires and card purchases are still approved on the approvals page
    (WEB_ONLY_APPROVAL_ACTIONS), so for those he asks and then says where to
    settle it; a spoken no still rejects it right away.
    """
    said = spoken_summary(
        approval["action_type"], approval["summary"], approval.get("payload") or {}
    )
    if approval["action_type"] in WEB_ONLY_APPROVAL_ACTIONS:
        return (
            f"I need your decision on something. {said} "
            f"Say {WAKE_WORD_DISPLAY} and tell me no to turn it down, or approve "
            "it on the approvals page at localhost port 8000 slash approvals."
        )
    return (
        f"I need your approval for something. {said} "
        f"Say {WAKE_WORD_DISPLAY} when you want to answer, and tell me yes or no."
    )
