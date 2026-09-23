"""The tool layer is Gary's only interface: check it exposes nothing dangerous,
converts times for the user, and guards voice approvals."""

from gary.tools import REGISTRY, TOOL_NAMES, TOOL_SCHEMAS, ToolContext, call_tool

from conftest import run

EXPECTED_TOOLS = {
    "project_create",
    "project_create_with_tasks",
    "project_list",
    "project_get",
    "project_update",
    "task_create",
    "task_update",
    "task_complete",
    "task_list",
    "task_get",
    "task_add_dependency",
    "task_remove_dependency",
    "followup_create",
    "followup_complete",
    "commitment_create",
    "commitment_update",
    "commitment_list",
    "followup_list_due",
    "planning_get_brief",
    "planning_find_work_blocks",
    "planning_run_cycle",
    "planning_get_context",
    "planning_record_plan",
    "action_propose",
    "approval_list_pending",
    "approval_resolve",
    "team_list",
    "delegate_to_agent",
    "run_management_review",
    "management_review_follow_up",
    "agent_assignment_get",
    "agent_assignments_list",
    "management_review_get",
    "engineering_create_ticket",
    "engineering_get_ticket",
    "engineering_list_tickets",
    "engineering_mark_ready",
    "engineering_mark_in_progress",
    "engineering_mark_review",
    "engineering_mark_security_review",
    "engineering_mark_done",
    "engineering_mark_blocked",
    "engineering_add_comment",
    "engineering_extend_spec",
    "engineering_sync",
    "engineering_status",
    "engineering_set_priority",
    "hiring_context",
    "propose_new_employee",
    "org_chart",
    "propose_reorganisation",
    "ask_user",
    "spoken_recent",
    "spoken_repeat",
    "question_answer",
}


def call(ctx, name, **arguments):
    return run(call_tool(name, arguments, ctx))


def test_exact_tool_surface():
    assert TOOL_NAMES == EXPECTED_TOOLS
    forbidden = ("sql", "shell", "delete", "policy", "audit", "permission", "schema")
    for name in TOOL_NAMES:
        assert not any(word in name for word in forbidden), name


def test_tool_schemas_are_realtime_function_tools():
    for schema in TOOL_SCHEMAS:
        assert schema["type"] == "function"
        assert schema["parameters"]["type"] == "object"
        assert schema["parameters"]["additionalProperties"] is False
        assert set(schema["parameters"]["required"]) <= set(schema["parameters"]["properties"])
    assert len(REGISTRY) == len(TOOL_SCHEMAS)


def test_tools_present_local_times_and_report_errors(gary):
    ctx = ToolContext(gary, {})
    created = call(ctx, "task_create", title="Film demo", deadline="2026-10-10T22:00:00Z")
    assert created["success"] is True
    assert created["task"]["deadline"] == "2026-10-10T17:00:00-05:00"  # America/Chicago

    bad = call(ctx, "task_create", title="Film demo", priority=42)
    assert bad["success"] is False
    assert "priority" in bad["error"]

    unknown_id = "7d0f4d1c-7e2b-4c55-9d1c-0b1e7f0e9a11"
    missing = call(ctx, "task_get", task_id=unknown_id)
    assert missing == {"success": False, "error": f"No task with id {unknown_id}"}
    not_an_id = call(ctx, "task_get", task_id="nope")
    assert not_an_id["success"] is False and "must be an id" in not_an_id["error"]


def test_voice_approval_requires_confirmation_and_known_id(gary, external):
    ctx = ToolContext(gary, {})
    proposed = call(
        ctx,
        "action_propose",
        action_type="send_external_email",
        payload={"to": "sam@example.com", "subject": "Draft", "body": "Hi"},
        reason="Commitment due today",
    )
    approval_id = proposed["approval_id"]

    unconfirmed = call(ctx, "approval_resolve", approval_id=approval_id, decision="approved", confirmed=False)
    assert unconfirmed["success"] is False
    assert external.sent_emails == []

    other_conversation = ToolContext(gary, {})
    unknown = call(
        other_conversation, "approval_resolve", approval_id=approval_id, decision="approved", confirmed=True
    )
    assert unknown["success"] is False
    assert "Unknown approval_id" in unknown["error"]
    assert external.sent_emails == []

    call(other_conversation, "approval_list_pending")
    approved = call(
        other_conversation, "approval_resolve", approval_id=approval_id, decision="approved", confirmed=True
    )
    assert approved["success"] is True
    assert approved["execution"]["status"] == "succeeded"
    assert external.sent_emails == ["sam@example.com"]

    with gary.db.read() as conn:
        details = conn.execute(
            "SELECT details_json FROM audit_log WHERE event_type = 'approval_approved'"
        ).fetchone()["details_json"]
    assert '"channel": "voice"' in details


def test_planning_context_tool_registers_pending_approvals(gary):
    proposer = ToolContext(gary, {})
    proposed = call(
        proposer,
        "action_propose",
        action_type="send_external_email",
        payload={"to": "sam@example.com", "subject": "Draft", "body": "Hi"},
        reason="x",
    )
    later = ToolContext(gary, {})
    context = call(later, "planning_get_context")
    assert context["context"]["pending_approvals"][0]["payload"]["to"] == "sam@example.com"
    assert proposed["approval_id"] in later.approval_ids()


def test_tools_reject_unexpected_arguments(gary):
    ctx = ToolContext(gary, {})
    assert call(ctx, "planning_get_context", sql="SELECT 1")["success"] is False
    assert call(ctx, "project_list", include_closed=True, drop=True)["success"] is False
