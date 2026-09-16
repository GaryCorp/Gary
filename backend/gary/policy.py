"""Approval policy as code.

Gary never chooses a risk level. The application looks the action type up
here; an action handler may only raise the level (e.g. moving a critical
event needs approval), never lower it. No tool can read or change this module.
"""

GREEN = "green"    # executes automatically
YELLOW = "yellow"  # needs the user's approval first
RED = "red"        # rejected by policy

ACTION_POLICIES = {
    "create_internal_task": GREEN,
    "update_internal_task": GREEN,
    "schedule_task": GREEN,
    "move_calendar_event": GREEN,  # escalated to yellow for critical events
    "send_external_email": YELLOW,
    "cancel_external_meeting": YELLOW,
    "download_file": YELLOW,
    "spend_money": RED,
    "change_security_settings": RED,
    "access_password_manager": RED,
    "change_own_permissions": RED,
}

RISK_ORDER = {GREEN: 0, YELLOW: 1, RED: 2}

# Moving a calendar event for a task at or above this priority, or one tied
# to an open commitment, is critical and needs approval.
CRITICAL_TASK_PRIORITY = 8

# Pending approvals expire after this long, so a stale request cannot be
# approved days later when circumstances have changed.
APPROVAL_EXPIRY_HOURS = 72

# The human principal, as recorded in the audit log.
USER_ACTOR = "alex"
GARY_ACTOR = "gary"
SYSTEM_ACTOR = "system"


def policy_risk(action_type: str) -> str | None:
    return ACTION_POLICIES.get(action_type)


def escalate(base: str, proposed: str | None) -> str:
    """The stricter of two risk levels."""
    if proposed is None:
        return base
    return max(base, proposed, key=RISK_ORDER.__getitem__)
