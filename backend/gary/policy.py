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
    "create_followup": GREEN,
    "schedule_task": GREEN,
    # Commissioning a specialist's report costs model tokens but changes
    # nothing by itself, so it runs without approval, under the caps in
    # roster.py and, for scheduled cycles, planning_cycle.py.
    "delegate_to_agent": GREEN,
    "run_management_review": GREEN,
    # Gary speaking first. It changes nothing by itself, so it is green; what
    # it spends is Alex's attention, which conversation_service.py caps.
    "ask_user": GREEN,
    # Moving a non-critical task block is green; a critical one (see
    # CRITICAL_TASK_PRIORITY) is escalated to yellow by its handler.
    "move_calendar_event": GREEN,
    "send_external_email": YELLOW,
    "change_external_commitment": YELLOW,
    "cancel_external_meeting": YELLOW,
    "download_file": YELLOW,
    # Catherine's debit card purchase requests. Always yellow, and approvable
    # only on the web page (WEB_ONLY_APPROVAL_ACTIONS). Any other way of
    # spending money stays red.
    "card_purchase": YELLOW,
    # Hiring a new AI employee. Yellow and web-only: a new colleague is added
    # to the roster only by Alex, deliberately, on the approvals page. What a
    # hire may do is capped in code (agents/hiring.py HIREABLE_TOOLS).
    "hire_employee": YELLOW,
    "spend_money": RED,
    "change_security_settings": RED,
    "access_password_manager": RED,
    "change_own_permissions": RED,
    "modify_permissions": RED,
    "delete_audit_log": RED,
}

RISK_ORDER = {GREEN: 0, YELLOW: 1, RED: 2}

# Approvals for these actions can only be approved on the /approvals web page,
# never by voice, where a misheard or injected "yes" could approve. Rejecting
# is allowed on any channel.
WEB_ONLY_APPROVAL_ACTIONS = frozenset({"card_purchase", "hire_employee"})

# Moving a calendar event for a task at or above this priority, or one tied
# to an open commitment, is critical and needs approval.
CRITICAL_TASK_PRIORITY = 8

# Pending approvals expire after this long, so a stale request cannot be
# approved days later when circumstances have changed.
APPROVAL_EXPIRY_HOURS = 72

# The human principal, as recorded in the audit log.
USER_ACTOR = "alex"
GARY_ACTOR = "gary"
CFO_ACTOR = "catherine"
SYSTEM_ACTOR = "system"


def policy_risk(action_type: str) -> str | None:
    return ACTION_POLICIES.get(action_type)


def escalate(base: str, proposed: str | None) -> str:
    """The stricter of two risk levels."""
    if proposed is None:
        return base
    return max(base, proposed, key=RISK_ORDER.__getitem__)
