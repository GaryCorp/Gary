"""Issue bodies and labels.

The body is the engineering specification Alex (and Claude Code) works from:
objective, requirements, acceptance criteria, security requirements,
dependencies, estimate, deadline, and the Gary task id that links it back to
SQLite. It says what the company needs, not how to implement it.

Nothing secret goes into an issue: no tokens, keys, passwords, customer data,
or credential contents. The repository is private, but the body still carries
only what engineering work needs.
"""

import logging
import re

from gary.integrations.github.client import GitHubClient
from gary.integrations.github.models import BASE_LABELS, LABEL_COLORS

logger = logging.getLogger("gary.github.issues")

TITLE_LIMIT = 240
BODY_LIMIT = 60_000
ITEM_LIMIT = 500

# Text that looks like a credential never reaches an issue body.
_SECRET_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?i)\b(api[_-]?key|secret|password|token)\b\s*[:=]\s*\S+"),
)
REDACTION = "[redacted]"


def scrub(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTION, text)
    return text


def _lines(items, bullet: str = "-") -> str:
    kept = [" ".join(str(item).split())[:ITEM_LIMIT] for item in items if str(item).strip()]
    return "\n".join(f"{bullet} {item}" for item in kept) if kept else "- None"


def render_issue_body(
    *,
    objective: str,
    requirements: list[str],
    acceptance_criteria: list[str],
    priority: str,
    task_id: str,
    project_name: str | None = None,
    security_requirements: str | None = None,
    dependencies: list[str] | None = None,
    estimated_minutes: int | None = None,
    due_at_local: str | None = None,
    requested_by: str = "Gary — Chief of Staff",
    assigned_to: str = "Alex — Software / AI Engineer",
) -> str:
    estimate = (
        f"{estimated_minutes // 60}h {estimated_minutes % 60}m"
        if estimated_minutes
        else "Not estimated"
    )
    body = f"""# Objective

{" ".join(objective.split())}

## Requested By

{requested_by}

## Assigned To

{assigned_to}

## Priority

{priority}

## Project

{project_name or "Not linked to a GaryCorp project"}

## Requirements

{_lines(requirements)}

## Acceptance Criteria

{_lines(acceptance_criteria, bullet="- [ ]")}

## Security Requirements

{" ".join((security_requirements or "Standard review").split())}

## Dependencies

{_lines(dependencies or [])}

## Estimated Effort

{estimate}

## Due

{due_at_local or "No deadline set"}

---

Gary Task ID: `{task_id}`
"""
    return scrub(body)[:BODY_LIMIT]


def render_spec_addition(
    *,
    requirements: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    note: str | None = None,
    added_at_local: str = "",
) -> str:
    """What Gary learned after the issue was filed, as a section appended to
    the body. The original specification is never rewritten: what was asked
    for first stays readable underneath what was added."""
    lines = [f"## Added by Gary{f' — {added_at_local}' if added_at_local else ''}"]
    if note:
        lines += ["", scrub(" ".join(str(note).split())[:ITEM_LIMIT])]
    if requirements:
        lines += ["", "**Also required**", scrub(_lines(requirements))]
    if acceptance_criteria:
        lines += ["", "**Also done when**", scrub(_lines(acceptance_criteria))]
    return "\n".join(lines)


def render_title(title: str) -> str:
    return scrub(" ".join(title.split()))[:TITLE_LIMIT]


def ticket_labels(
    priority: str, kind: str = "feature", security_review_required: bool = False
) -> list[str]:
    # A video stage is not engineering work, so it does not say it is.
    base = [label for label in BASE_LABELS if not (kind == "production" and label == "engineering")]
    labels = [*base, kind, priority]
    if security_review_required:
        labels.append("security-review")
    # Stable order, no duplicates.
    return list(dict.fromkeys(labels))


async def ensure_labels(client: GitHubClient, labels: list[str]) -> dict[str, list[str]]:
    """Create the labels the repository is missing, reusing any that exist."""
    existing = {name.casefold() for name in await client.list_labels()}
    created, reused = [], []
    for label in labels:
        if label.casefold() in existing:
            reused.append(label)
            continue
        if await client.create_label(label, LABEL_COLORS.get(label, "ededed"), "GaryCorp"):
            created.append(label)
        else:
            reused.append(label)
    return {"created": created, "reused": reused}
