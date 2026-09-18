"""Engineering tickets: privacy gate, GitHub client behavior, ticket creation,
state transitions, synchronization, audit, and Gary's tools. GitHub is faked;
no test touches the network or creates a real issue.
"""

import asyncio
import json

import pytest

from gary.db.repositories import Repositories
from gary.integrations.github.client import GitHubClient
from gary.integrations.github.config import EnvTokenProvider, GitHubConfig, configured
from gary.integrations.github.exceptions import (
    GitHubAuthError,
    GitHubPermissionError,
    GitHubPrivacyError,
    GitHubRateLimitError,
    GitHubUnavailableError,
)
from gary.integrations.github.issues import render_issue_body, scrub, ticket_labels
from gary.integrations.github.models import ALLOWED_TRANSITIONS, EngineeringStatus
from gary.integrations.github.privacy import PrivacyGate
from gary.integrations.github.projects import ProjectBoard
from gary.models.engineering import CreateEngineeringTicketRequest
from gary.services.engineering_service import EngineeringError, EngineeringTicketService
from gary.tools import ToolContext, call_tool

from conftest import make_project, make_task
from fake_github import FakeGitHub

TOKEN = "ghp_secrettokenvalue1234567890abcd"


def run(coroutine):
    return asyncio.run(coroutine)


def build_config(**overrides) -> GitHubConfig:
    data = {
        "owner": "garycorp",
        "repository": "gary",
        "project_number": 7,
        "engineer_username": "alex",
    }
    data.update(overrides)
    return GitHubConfig(**data)


def build_service(gary, github: FakeGitHub | None = None, clock=None, **config_overrides):
    github = github or FakeGitHub()
    config = build_config(**config_overrides)
    client = GitHubClient(
        config, EnvTokenProvider(TOKEN), transport=github, sleep=_no_sleep
    )
    service = EngineeringTicketService(
        gary, client, board=ProjectBoard(client), gate=PrivacyGate(client, cache_seconds=0),
        clock=clock or gary.planning.clock,
    )
    return service, github


async def _no_sleep(_seconds):
    return None


def ticket_request(task, **overrides) -> CreateEngineeringTicketRequest:
    data = {
        "task_id": task["id"],
        "title": "Add read-only research integration",
        "objective": "Susan needs read-only web research so her reports cite current sources.",
        "requirements": ["Read-only web search", "No credentials stored"],
        "acceptance_criteria": ["Susan can cite sources", "No write access exists"],
        "priority": "P1",
    }
    data.update(overrides)
    return CreateEngineeringTicketRequest(**data)


def create(service, task, **overrides):
    return run(service.create_ticket(ticket_request(task, **overrides)))


def ticket_rows(gary) -> list[dict]:
    with gary.db.read() as conn:
        return Repositories.bind(conn).engineering.list_all()


def audit_for(gary, ticket_id: str) -> list[str]:
    with gary.db.read() as conn:
        rows = conn.execute(
            "SELECT event_type FROM audit_log WHERE entity_id = ? ORDER BY id", (ticket_id,)
        ).fetchall()
    return [row["event_type"] for row in rows]


# ------------------------------------------------------------------ privacy

def test_private_repository_and_project_pass_the_gate(gary):
    service, github = build_service(gary)
    repository, project = run(service.gate.verify())
    assert repository.private is True and project.private is True
    report = run(service.audit_github_privacy())
    assert report == {
        "repository_private": True,
        "project_private": True,
        "owner_matches": True,
        "project_owner_matches": True,
        "safe_to_operate": True,
        "detail": report["detail"],
    }


def test_public_repository_is_refused_before_anything_is_created(gary):
    service, github = build_service(gary, FakeGitHub(private=False))
    task = make_task(gary, title="Research integration")

    with pytest.raises(GitHubPrivacyError, match="is public"):
        create(service, task)

    assert github.issues == {} and github.items == {}
    row = ticket_rows(gary)[0]
    assert row["github_issue_number"] is None and row["sync_state"] == "pending"
    assert "github_privacy_check_failed" in audit_for(gary, row["id"])
    report = run(service.audit_github_privacy())
    assert report["repository_private"] is False and report["safe_to_operate"] is False


def test_public_project_is_refused_and_no_issue_is_created(gary):
    service, github = build_service(gary, FakeGitHub(project_public=True))
    task = make_task(gary, title="Research integration")

    with pytest.raises(GitHubPrivacyError, match="Project"):
        create(service, task)
    assert github.issues == {}
    report = run(service.audit_github_privacy())
    assert report["project_private"] is False and report["safe_to_operate"] is False


def test_privacy_fails_closed_when_github_is_unreachable(gary):
    service, github = build_service(gary)
    github.network_error_on.add("get_repository")
    report = run(service.audit_github_privacy())
    assert report["safe_to_operate"] is False
    status = run(service.status())
    assert status["state"] == "unavailable"


def test_wrong_owner_is_refused(gary):
    github = FakeGitHub()
    github.reported_owner = "someone-else"
    service, _ = build_service(gary, github)
    with pytest.raises(GitHubPrivacyError, match="GITHUB_OWNER"):
        run(service.gate.verify())


# ------------------------------------------------------------------- client

def test_api_errors_are_mapped(gary):
    service, github = build_service(gary)
    for status, error in (
        (401, GitHubAuthError),
        (403, GitHubPermissionError),
        (429, GitHubRateLimitError),
        (500, GitHubUnavailableError),
    ):
        github.fail["get_repository"] = (status, "boom")
        with pytest.raises(error):
            run(service.client.get_repository())
    github.fail.clear()
    github.network_error_on.add("get_repository")
    with pytest.raises(GitHubUnavailableError):
        run(service.client.get_repository())


def test_transient_failures_are_retried_and_permission_failures_are_not(gary):
    service, github = build_service(gary)
    github.fail["get_repository"] = (503, "unavailable")
    with pytest.raises(GitHubUnavailableError):
        run(service.client.get_repository())
    assert github.calls["get_repository"] == 3  # bounded retries

    github.calls.clear()
    github.fail["get_repository"] = (403, "Resource not accessible by integration")
    with pytest.raises(GitHubPermissionError):
        run(service.client.get_repository())
    assert github.calls["get_repository"] == 1  # not retried


def test_token_is_sent_but_never_logged_or_stored(gary, caplog):
    import logging

    service, github = build_service(gary)
    task = make_task(gary, title="Research integration")
    with caplog.at_level(logging.DEBUG, logger="gary.github"):
        create(service, task)

    assert any(header == f"Bearer {TOKEN}" for header in github.auth_headers)
    assert TOKEN not in caplog.text
    assert "[redacted]" in caplog.text

    with gary.db.read() as conn:
        dump = "\n".join(
            str(dict(row))
            for table in ("engineering_tickets", "audit_log", "github_project_fields")
            for row in conn.execute(f"SELECT * FROM {table}")
        )
    assert TOKEN not in dump and "ghp_" not in dump
    assert TOKEN not in github.issue_body(1)


def test_secrets_are_scrubbed_from_issue_text():
    body = render_issue_body(
        objective="Rotate the key",
        requirements=["Set GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz012345 in the env"],
        acceptance_criteria=["No secret in the repository"],
        priority="P1",
        task_id="task-uuid",
    )
    assert "ghp_abcdefghijklmnopqrstuvwxyz012345" not in body
    assert "[redacted]" in body
    assert scrub("api_key: hunter2") == "[redacted]"


# ------------------------------------------------------------------ creation

def test_create_ticket_end_to_end(gary):
    service, github = build_service(gary)
    project = make_project(gary, name="Specialist team")
    task = make_task(gary, title="Add read-only research integration", project_id=project["id"])

    ticket = create(service, task, security_review_required=True, estimated_minutes=180)

    assert ticket.github_issue_number == 1
    assert ticket.status is EngineeringStatus.READY
    assert ticket.assignment_confirmed is True
    assert ticket.sync_state == "synced"
    assert ticket.github_url.endswith("/issues/1")

    issue = github.issues[1]
    assert issue["assignees"] == ["alex"]
    assert set(issue["labels"]) == set(ticket_labels("P1", "feature", True))
    body = issue["body"]
    assert "# Objective" in body and "## Acceptance Criteria" in body
    assert f"Gary Task ID: `{task['id']}`" in body
    assert "Specialist team" in body

    item_id = ticket.github_project_item_id
    assert github.status_of(item_id) == "Ready"

    row = ticket_rows(gary)[0]
    assert row["task_id"] == task["id"]
    assert row["github_project_item_id"] == item_id
    assert row["github_issue_node_id"] == issue["node_id"]

    events = audit_for(gary, ticket.id)
    assert events[:5] == [
        "github_issue_created",
        "engineering_ticket_created",
        "engineering_ticket_assigned",
        "github_project_item_added",
        "engineering_status_changed",
    ]


def test_labels_are_created_once_and_reused(gary):
    service, github = build_service(gary)
    github.labels["engineering"] = "1d76db"  # already exists
    first = make_task(gary, title="First")
    second = make_task(gary, title="Second")

    create(service, first)
    created_after_first = set(github.labels)
    create(service, second)

    assert "engineering" in created_after_first
    assert set(github.labels) == created_after_first  # nothing duplicated
    assert github.calls["create_label"] == len(created_after_first) - 1


def test_one_ticket_per_task(gary):
    service, github = build_service(gary)
    task = make_task(gary, title="Research integration")
    create(service, task)

    with pytest.raises(EngineeringError, match="already has engineering ticket"):
        create(service, task)
    assert len(github.issues) == 1
    assert len(ticket_rows(gary)) == 1


def test_closed_or_missing_task_is_refused(gary):
    service, _ = build_service(gary)
    task = make_task(gary, title="Done already")
    gary.tasks.complete_task.__self__  # service exists
    from gary.models.task import CompleteTaskRequest

    gary.tasks.complete_task(CompleteTaskRequest(task_id=task["id"]))
    with pytest.raises(EngineeringError, match="completed"):
        create(service, task)


def test_assignment_failure_is_reported_not_claimed(gary):
    service, github = build_service(gary, FakeGitHub(assignable=()))
    task = make_task(gary, title="Research integration")

    ticket = create(service, task)

    assert ticket.github_issue_number == 1  # the issue is still useful
    assert ticket.assignment_confirmed is False
    assert ticket.sync_state == "degraded"
    assert "alex" in ticket.sync_error
    assert "github_sync_failed" in audit_for(gary, ticket.id)


def test_partial_creation_is_retried_without_a_second_issue(gary):
    service, github = build_service(gary)
    task = make_task(gary, title="Research integration")
    github.fail["add_item"] = (500, "GitHub is having a moment")

    ticket = create(service, task)
    assert ticket.github_issue_number == 1
    assert ticket.github_project_item_id is None
    assert ticket.sync_state == "degraded"
    assert ticket.status is EngineeringStatus.BACKLOG  # never claimed Ready

    github.fail.clear()
    retried = run(service.retry_incomplete(ticket.id))

    assert len(github.issues) == 1  # the same issue, not a second one
    assert retried.github_project_item_id is not None
    assert retried.status is EngineeringStatus.READY
    assert retried.sync_state == "synced"
    assert github.status_of(retried.github_project_item_id) == "Ready"


def test_issue_creation_failure_leaves_no_half_ticket(gary):
    service, github = build_service(gary)
    task = make_task(gary, title="Research integration")
    github.fail["create_issue"] = (422, "Validation Failed")

    with pytest.raises(Exception):
        create(service, task)
    row = ticket_rows(gary)[0]
    assert row["github_issue_number"] is None and row["sync_state"] == "pending"

    github.fail.clear()
    ticket = create(service, task)  # the reserved row is reused
    assert ticket.github_issue_number == 1
    assert len(ticket_rows(gary)) == 1


def test_optional_project_fields_are_used_when_present(gary):
    github = FakeGitHub()
    github.extra_fields = {
        "Priority": {"id": "F_priority", "dataType": "SINGLE_SELECT",
                     "options": {"P0": "p0", "P1": "p1", "P2": "p2", "P3": "p3"}},
        "Department": {"id": "F_dept", "dataType": "SINGLE_SELECT",
                       "options": {"Engineering": "dept-eng"}},
        "Estimate": {"id": "F_estimate", "dataType": "NUMBER"},
        "Gary Task ID": {"id": "F_task", "dataType": "TEXT"},
    }
    service, _ = build_service(gary, github)
    task = make_task(gary, title="Research integration")

    ticket = create(service, task, estimated_minutes=180)
    fields = github.items[ticket.github_project_item_id]["fields"]
    assert fields["Priority"] == "P1"
    assert fields["Department"] == "Engineering"
    assert fields["Estimate"] == 3.0
    assert fields["Gary Task ID"] == task["id"]


def test_missing_status_option_does_not_pretend_to_set_it(gary):
    service, github = build_service(gary, FakeGitHub(status_options=("Backlog", "Done")))
    task = make_task(gary, title="Research integration")

    ticket = create(service, task)
    assert ticket.github_issue_number == 1
    assert github.status_of(ticket.github_project_item_id) is None
    assert ticket.status is EngineeringStatus.BACKLOG
    assert ticket.sync_state == "degraded"
    assert "Ready" in ticket.sync_error


# --------------------------------------------------------------- transitions

def test_full_lifecycle_with_security_review(gary):
    service, github = build_service(gary)
    task = make_task(gary, title="Research integration")
    ticket = create(service, task, security_review_required=True)

    row = service.resolve(ticket_id=ticket.id)
    ticket = run(service.transition(row, EngineeringStatus.IN_PROGRESS, "Alex started"))
    assert github.status_of(ticket.github_project_item_id) == "In Progress"
    with gary.db.read() as conn:
        assert Repositories.bind(conn).tasks.get(task["id"])["status"] == "in_progress"

    for target, expected in (
        (EngineeringStatus.REVIEW, "Review"),
        (EngineeringStatus.SECURITY_REVIEW, "Security Review"),
        (EngineeringStatus.DONE, "Done"),
    ):
        ticket = run(service.transition(service.resolve(ticket_id=ticket.id), target))
        assert github.status_of(ticket.github_project_item_id) == expected

    assert github.issues[1]["state"] == "closed"
    with gary.db.read() as conn:
        assert Repositories.bind(conn).tasks.get(task["id"])["status"] == "completed"
    events = audit_for(gary, ticket.id)
    assert "engineering_ticket_completed" in events
    assert events.count("engineering_status_changed") >= 4
    assert any(c["body"].startswith("In Progress: Alex started") for c in github.comments)


def test_security_review_cannot_be_skipped(gary):
    service, github = build_service(gary)
    task = make_task(gary, title="Research integration")
    ticket = create(service, task, security_review_required=True)
    run(service.transition(service.resolve(ticket_id=ticket.id), EngineeringStatus.IN_PROGRESS))
    run(service.transition(service.resolve(ticket_id=ticket.id), EngineeringStatus.REVIEW))

    with pytest.raises(EngineeringError, match="security review"):
        run(service.transition(service.resolve(ticket_id=ticket.id), EngineeringStatus.DONE))

    assert github.issues[1]["state"] == "open"
    with gary.db.read() as conn:
        assert Repositories.bind(conn).tasks.get(task["id"])["status"] != "completed"


def test_review_to_done_allowed_without_security_review(gary):
    service, github = build_service(gary)
    task = make_task(gary, title="Small fix")
    ticket = create(service, task, security_review_required=False)
    for target in (EngineeringStatus.IN_PROGRESS, EngineeringStatus.REVIEW, EngineeringStatus.DONE):
        ticket = run(service.transition(service.resolve(ticket_id=ticket.id), target))
    assert ticket.status is EngineeringStatus.DONE
    assert github.issues[1]["state"] == "closed"


def test_invalid_transitions_are_refused(gary):
    service, _ = build_service(gary)
    task = make_task(gary, title="Research integration")
    ticket = create(service, task)

    with pytest.raises(EngineeringError, match="cannot go from"):
        run(service.transition(service.resolve(ticket_id=ticket.id), EngineeringStatus.DONE))
    with pytest.raises(EngineeringError, match="cannot go from"):
        run(service.transition(service.resolve(ticket_id=ticket.id), EngineeringStatus.REVIEW))
    assert EngineeringStatus.DONE not in ALLOWED_TRANSITIONS[EngineeringStatus.READY]
    assert ALLOWED_TRANSITIONS[EngineeringStatus.DONE] == frozenset()


def test_blocked_and_back(gary):
    service, github = build_service(gary, FakeGitHub(
        status_options=("Backlog", "Ready", "In Progress", "Review", "Security Review", "Done", "Blocked")
    ))
    task = make_task(gary, title="Research integration")
    ticket = create(service, task)
    run(service.transition(service.resolve(ticket_id=ticket.id), EngineeringStatus.IN_PROGRESS))

    ticket = run(service.transition(
        service.resolve(ticket_id=ticket.id), EngineeringStatus.BLOCKED, "Waiting on the API key"
    ))
    assert ticket.status is EngineeringStatus.BLOCKED
    assert "blocked" in github.issues[1]["labels"]
    assert github.status_of(ticket.github_project_item_id) == "Blocked"
    with gary.db.read() as conn:
        assert Repositories.bind(conn).tasks.get(task["id"])["status"] == "blocked"
    assert "engineering_ticket_blocked" in audit_for(gary, ticket.id)

    ticket = run(service.transition(service.resolve(ticket_id=ticket.id), EngineeringStatus.IN_PROGRESS))
    assert "blocked" not in github.issues[1]["labels"]


def test_comments_are_posted_and_audited(gary):
    service, github = build_service(gary)
    task = make_task(gary, title="Research integration")
    ticket = create(service, task)

    result = run(service.add_comment(
        service.resolve(ticket_id=ticket.id), "Priority increased because this blocks Episode 7."
    ))
    assert result["issue_number"] == 1
    assert github.comments[-1]["body"].startswith("Priority increased")
    assert "engineering_ticket_commented" in audit_for(gary, ticket.id)


# ------------------------------------------------------------ synchronization

def test_sync_adopts_github_status(gary):
    service, github = build_service(gary)
    task = make_task(gary, title="Research integration")
    ticket = create(service, task)

    # Alex moved the card on the board.
    github.items[ticket.github_project_item_id]["fields"]["Status"] = "In Progress"
    result = run(service.sync_ticket(ticket.id))

    assert result["status"] == "in_progress"
    assert service.get_ticket(ticket_id=ticket.id).status is EngineeringStatus.IN_PROGRESS
    with gary.db.read() as conn:
        assert Repositories.bind(conn).tasks.get(task["id"])["status"] == "in_progress"
    assert "engineering_status_changed" in audit_for(gary, ticket.id)


def test_sync_completes_the_task_when_done_and_closed(gary):
    service, github = build_service(gary)
    task = make_task(gary, title="Research integration")
    ticket = create(service, task)

    github.items[ticket.github_project_item_id]["fields"]["Status"] = "Done"
    github.issues[1]["state"] = "closed"
    result = run(service.sync_ticket(ticket.id))

    assert result["needs_reconciliation"] is False
    with gary.db.read() as conn:
        assert Repositories.bind(conn).tasks.get(task["id"])["status"] == "completed"
    assert "engineering_ticket_completed" in audit_for(gary, ticket.id)


def test_unexpected_closure_is_flagged_not_completed(gary):
    service, github = build_service(gary)
    task = make_task(gary, title="Research integration")
    ticket = create(service, task, security_review_required=True)

    github.issues[1]["state"] = "closed"  # closed while still Ready
    result = run(service.sync_ticket(ticket.id))

    assert result["needs_reconciliation"] is True
    row = service.resolve(ticket_id=ticket.id)
    assert row["sync_state"] == "needs_reconciliation"
    with gary.db.read() as conn:
        assert Repositories.bind(conn).tasks.get(task["id"])["status"] != "completed"
    assert "github_sync_failed" in audit_for(gary, ticket.id)


def test_done_without_security_review_is_flagged_on_close(gary):
    service, github = build_service(gary)
    task = make_task(gary, title="Research integration")
    ticket = create(service, task, security_review_required=True)

    github.items[ticket.github_project_item_id]["fields"]["Status"] = "Done"
    github.issues[1]["state"] = "closed"
    result = run(service.sync_ticket(ticket.id))

    assert result["needs_reconciliation"] is True
    with gary.db.read() as conn:
        assert Repositories.bind(conn).tasks.get(task["id"])["status"] != "completed"


def test_sync_records_assignee_change_and_failures(gary):
    service, github = build_service(gary)
    task = make_task(gary, title="Research integration")
    ticket = create(service, task)

    github.issues[1]["assignees"] = []
    result = run(service.sync_ticket(ticket.id))
    assert result["assignment_confirmed"] is False

    github.fail["get_issue"] = (500, "boom")
    failed = run(service.sync_ticket(ticket.id))
    assert failed["synced"] is False
    assert service.resolve(ticket_id=ticket.id)["sync_state"] == "degraded"
    assert "github_sync_failed" in audit_for(gary, ticket.id)


def test_sync_all_reports_each_ticket(gary):
    service, github = build_service(gary)
    first = create(service, make_task(gary, title="One"))
    second = create(service, make_task(gary, title="Two"))
    github.items[second.github_project_item_id]["fields"]["Status"] = "Review"

    result = run(service.sync_all())
    assert result["checked"] == 2 and result["synced"] == 2
    assert service.get_ticket(ticket_id=second.id).status is EngineeringStatus.REVIEW
    assert service.get_ticket(ticket_id=first.id).status is EngineeringStatus.READY


# ---------------------------------------------------------------- Gary tools

def tools_ctx(gary, service) -> ToolContext:
    return ToolContext(gary, {}, {"engineering": service})


def test_gary_tools_create_read_and_move(gary):
    service, github = build_service(gary)
    ctx = tools_ctx(gary, service)
    task = make_task(gary, title="Add read-only research integration")

    async def scenario():
        created = await call_tool(
            "engineering_create_ticket",
            {
                "task_id": task["id"],
                "title": "Add read-only research integration",
                "objective": "Susan needs read-only web research for sourced reports.",
                "requirements": ["Read-only search"],
                "acceptance_criteria": ["Susan cites sources"],
                "priority": "P1",
                "security_review_required": True,
            },
            ctx,
        )
        listed = await call_tool("engineering_list_tickets", {}, ctx)
        moved = await call_tool(
            "engineering_mark_in_progress", {"task_id": task["id"], "reason": "Alex started"}, ctx
        )
        early_done = await call_tool("engineering_mark_done", {"task_id": task["id"]}, ctx)
        fetched = await call_tool("engineering_get_ticket", {"issue_number": 1}, ctx)
        status = await call_tool("engineering_status", {}, ctx)
        return created, listed, moved, early_done, fetched, status

    created, listed, moved, early_done, fetched, status = run(scenario())

    assert created["success"] is True
    assert created["ticket"]["issue_number"] == 1
    assert created["ticket"]["status"] == "ready"
    assert "warning" not in created["ticket"]
    assert listed["count"] == 1
    assert moved["status"] == "in_progress"
    assert early_done["success"] is False and "cannot go from" in early_done["error"]
    assert fetched["ticket"]["ticket_id"] == created["ticket"]["ticket_id"]
    assert status["github_engineering_status"]["state"] == "healthy"


def test_gary_tools_report_github_failures_as_failures(gary):
    service, github = build_service(gary, FakeGitHub(private=False))
    ctx = tools_ctx(gary, service)
    task = make_task(gary, title="Research integration")

    result = run(call_tool(
        "engineering_create_ticket",
        {
            "task_id": task["id"],
            "title": "Add read-only research integration",
            "objective": "Susan needs read-only web research for sourced reports.",
            "requirements": ["Read-only search"],
            "acceptance_criteria": ["Susan cites sources"],
        },
        ctx,
    ))
    assert result["success"] is False
    assert "public" in result["error"]
    assert github.issues == {}


def test_gary_tool_warns_when_assignment_is_unconfirmed(gary):
    service, _ = build_service(gary, FakeGitHub(assignable=()))
    ctx = tools_ctx(gary, service)
    task = make_task(gary, title="Research integration")

    result = run(call_tool(
        "engineering_create_ticket",
        {
            "task_id": task["id"],
            "title": "Add read-only research integration",
            "objective": "Susan needs read-only web research for sourced reports.",
            "requirements": ["Read-only search"],
            "acceptance_criteria": ["Susan cites sources"],
        },
        ctx,
    ))
    assert result["success"] is True
    assert "warning_assignment" in result["ticket"]
    assert result["ticket"]["assignment_confirmed"] is False


def test_gary_has_no_raw_github_or_admin_tools():
    from gary.tools import REGISTRY

    forbidden = (
        "github_raw_request", "graphql_execute", "delete_repository", "change_visibility",
        "manage_collaborators", "modify_permissions", "github_request", "create_repository",
        "publish_package", "enable_pages",
    )
    assert not set(forbidden) & set(REGISTRY)
    assert {name for name in REGISTRY if name.startswith("engineering_")} == {
        "engineering_create_ticket", "engineering_get_ticket", "engineering_list_tickets",
        "engineering_mark_ready", "engineering_mark_in_progress", "engineering_mark_review",
        "engineering_mark_security_review", "engineering_mark_done", "engineering_mark_blocked",
        "engineering_add_comment", "engineering_sync", "engineering_status",
    }


def test_integration_absent_is_reported_not_crashed(gary):
    ctx = ToolContext(gary, {}, {})
    result = run(call_tool("engineering_status", {}, ctx))
    assert result["github_engineering_status"]["state"] == "unavailable"
    created = run(call_tool("engineering_list_tickets", {}, ctx))
    assert created["success"] is False and "not available" in created["error"]


# ------------------------------------------------------------- configuration

def test_config_requires_valid_values():
    from gary.integrations.github.exceptions import GitHubConfigurationError

    assert build_config().full_name == "garycorp/gary"
    for bad in ({"owner": ""}, {"repository": "not a repo"}, {"project_number": 0},
                {"engineer_username": "-nope-"}):
        with pytest.raises(GitHubConfigurationError):
            build_config(**bad)
    with pytest.raises(GitHubConfigurationError):
        EnvTokenProvider("")

    env = {
        "GITHUB_TOKEN": TOKEN,
        "GITHUB_OWNER": "garycorp",
        "GITHUB_REPOSITORY": "gary",
        "GITHUB_PROJECT_NUMBER": "7",
        "GITHUB_ENGINEER_USERNAME": "alex",
    }
    assert configured(env) is True
    assert GitHubConfig.from_env(env).project_number == 7
    assert configured({**env, "GITHUB_TOKEN": ""}) is False


def test_migration_keeps_one_ticket_per_task(gary):
    task = make_task(gary, title="Research integration")
    with gary.db.transaction() as conn:
        repos = Repositories.bind(conn)
        repos.engineering.create(task["id"], "garycorp", "gary", "alex", "P1")
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        with gary.db.transaction() as conn:
            Repositories.bind(conn).engineering.create(task["id"], "garycorp", "gary", "alex", "P1")


def test_project_field_ids_are_cached_not_rediscovered(gary):
    service, github = build_service(gary)
    from gary.services.engineering_service import _FieldCache

    service.board = ProjectBoard(service.client, cache=_FieldCache(gary))
    create(service, make_task(gary, title="One"))
    discovered = github.calls["get_project_fields"]

    service.board = ProjectBoard(service.client, cache=_FieldCache(gary))
    create(service, make_task(gary, title="Two"))
    assert github.calls["get_project_fields"] == discovered  # served from SQLite

    with gary.db.read() as conn:
        cached = Repositories.bind(conn).project_fields.load(github.project_id)
    assert cached["Status"]["id"] == "F_status"
    assert json.dumps(cached).count("opt-") >= 6


def test_missing_repository_error_says_what_to_check(gary):
    """A 404 on the repository is ambiguous at GitHub's end; the message must
    name the likely causes instead of passing on 'Not Found'."""
    service, github = build_service(gary, repository="does-not-exist")
    with pytest.raises(Exception) as exc:
        run(service.client.get_repository())
    message = str(exc.value)
    assert "garycorp/does-not-exist" in message
    assert "GITHUB_OWNER" in message and "Metadata: read" in message


def test_whoami_reports_the_authenticated_account(gary):
    service, github = build_service(gary)
    assert run(service.client.whoami()) == "garycorp-bot"


@pytest.mark.parametrize("owner_type", ["organization", "user"])
def test_project_is_found_for_either_owner_type(gary, owner_type):
    """GraphQL reports the owner kind that does not exist as an error, so each
    kind is asked for separately; a combined query always looks like a failure."""
    service, github = build_service(gary, FakeGitHub(owner_type=owner_type))
    project = run(service.client.get_project())
    assert project.number == 7 and project.private is True
    assert run(service.client.get_owner_id())[1] == owner_type

    ticket = create(service, make_task(gary, title="Research integration"))
    assert ticket.status is EngineeringStatus.READY
    assert github.status_of(ticket.github_project_item_id) == "Ready"


def test_missing_project_number_is_reported_clearly(gary):
    service, _ = build_service(gary, FakeGitHub(), project_number=42)
    with pytest.raises(Exception, match="No Project number 42"):
        run(service.client.get_project())
