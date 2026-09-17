# Engineering tickets (internal)

**GaryCorp is proprietary and not open source.** This integration only works
with a **private** repository and a **private** GitHub Project, and refuses to
do anything if either is public. Nothing in it can change visibility, publish
anything, or write code.

Gary turns a company objective into an engineering specification: an internal
task in SQLite, a private GitHub issue assigned to Alex, and a card on the
private **GaryCorp Engineering** Project. Alex works the issue with Claude
Code; the Project's Status is how progress comes back to Gary.

## Architecture

```text
Gary (voice) ── engineering_create_ticket ─┐
                                           ▼
                              EngineeringTicketService
                     (SQLite is authoritative company state)
                                           │
              ┌────────────────────────────┼─────────────────────────┐
              ▼                            ▼                         ▼
        PrivacyGate                   GitHubClient               ProjectBoard
   repository private?          REST: issues, labels,       Projects v2 GraphQL:
   Project private?             assignees, comments         item + Status field
   owner matches?
              │                            │                         │
              └──────────── refuses everything if public ────────────┘
```

```text
Gary task (SQLite)  ⇄  engineering_tickets row  ⇄  private issue  ⇄  Project item
```

- **SQLite is the source of truth** for company planning: tasks, projects,
  scheduling, audit.
- **GitHub is the source of truth** for the issue's workflow state. A sync
  adopts GitHub's Project Status and open/closed state.
- `task_id` is the idempotency link: `UNIQUE(task_id)` means one task can never
  produce two GitHub issues.

### Files

| File | Purpose |
|---|---|
| `backend/gary/integrations/github/config.py` | Settings, `GitHubCredentialProvider`, `EnvTokenProvider` |
| `backend/gary/integrations/github/client.py` | REST + GraphQL client, error mapping, retries, header redaction |
| `backend/gary/integrations/github/privacy.py` | `PrivacyGate.verify()` and `audit()` |
| `backend/gary/integrations/github/projects.py` | Project board in Gary's status vocabulary; field-id cache |
| `backend/gary/integrations/github/issues.py` | Issue body template, label creation/reuse, secret scrubbing |
| `backend/gary/integrations/github/models.py` | `EngineeringStatus`, transitions, typed GitHub results |
| `backend/gary/integrations/github/exceptions.py` | Every failure mode, including `GitHubPrivacyError` |
| `backend/gary/integrations/github/setup.py` | `python -m gary.integrations.github.setup` |
| `backend/gary/services/engineering_service.py` | Ticket lifecycle, transitions, sync, audit |
| `backend/gary/db/repositories/engineering.py` | `engineering_tickets`, cached Project field ids |
| `backend/gary/db/migrations/004_engineering.sql` | Schema |
| `backend/gary/models/engineering.py` | Domain model and validated tool inputs |
| `backend/gary/tools/engineering_tools.py` | The 12 tools Gary has |

## Authentication

The credential is read from `GITHUB_TOKEN` in the environment and used only as
an `Authorization` header. It is **never** written to source code, prompts,
SQLite, Joplin, issue bodies, logs, CrewAI context, or a notebook. HTTP debug
logs print headers with the authorization value replaced by `[redacted]`, and
issue text runs through a scrubber that redacts anything shaped like a token,
key, or password.

`GitHubCredentialProvider` is a protocol with one call, `get_token()`. v1 ships
`EnvTokenProvider`; a GaryCorp GitHub App minting short-lived installation
tokens can replace it without touching the client.

### Minimum GitHub permissions

Use a **fine-grained** personal access token (or later, a GitHub App) scoped to
the one private repository:

| Scope | Permission | Why |
|---|---|---|
| Repository | **Metadata: read** | verify the repository is private |
| Repository | **Issues: write** | create, update, comment, label, assign |
| Organization | **Projects: write** | add items, set Status |

Do **not** grant Contents, Actions, Secrets, Workflows, Administration,
Deployments, Packages, Pages, or organization administration. Gary's ticket
credential deliberately cannot change source code, branch protection,
collaborators, releases, or visibility. Alex (and Claude Code) use a separate
development credential for code. Separation of duties is the point.

If Projects live on a personal account rather than an organization, the token
needs the equivalent user Projects write permission; the client resolves either
owner type.

## Private-only requirement

Before any write, `PrivacyGate.verify()` checks:

1. the repository exists and `private == true`;
2. the repository owner matches `GITHUB_OWNER`;
3. the Project exists and is **not** public;
4. the Project owner matches `GITHUB_OWNER`.

Any failure raises `GitHubPrivacyError` and the operation stops before an
issue, comment, or Project item is created. The check fails closed: if GitHub
cannot be reached, the answer is "not safe to operate", never "probably fine".
Results are cached for five minutes, so a repository flipped to public is
noticed quickly without re-reading on every call.

Gary can read the audit (`engineering_status`, `GET /engineering/status`) but
has no tool that changes visibility. Flipping a repository or Project back to
private is a manual step for Alex, by design.

## Setup

1. Create (or pick) the **private** repository and set `GITHUB_OWNER` and
   `GITHUB_REPOSITORY`.
2. Create a **private** organization Project called `GaryCorp Engineering`, add
   a `Status` single-select field with options **Backlog, Ready, In Progress,
   Review, Security Review, Done** (optionally **Blocked**), and set
   `GITHUB_PROJECT_NUMBER` from its URL.
3. Optionally add `Priority` (P0–P3), `Department`, `Estimate` (number, hours),
   and `Gary Task ID` (text) fields. They are used when present, skipped when
   absent.
4. Set `GITHUB_TOKEN` and `GITHUB_ENGINEER_USERNAME` in `.env` (never commit
   `.env`; it is git-ignored).
5. Verify:

```bash
docker compose exec backend python -m gary.integrations.github.setup
```

It checks authentication, that the repository is private and has issues
enabled, finds the Project and checks it is private, discovers the Status field
and option ids, checks the engineer can be assigned, and reports missing
labels. `--create-labels` creates the GaryCorp labels; `--create-project`
creates the Project **as private** if it does not exist. If anything that must
be private is public, it stops and explains, and changes nothing.

## Ticket lifecycle

```text
Gary identifies an engineering requirement
   → internal SQLite task
   → engineering_create_ticket
   → private issue, assigned to Alex, labelled
   → added to the private Engineering Project, Status = Ready
   → Gary schedules engineering time on the calendar (existing task tools)
   → Alex works the issue with Claude Code
   → Ready → In Progress → Review → [Security Review] → Done
   → issue closes, SQLite task completes, Gary records it
```

Creation runs in this order, and each step persists before the next, so a
failure part way can be retried without duplicating anything:

1. validate the task, reserve the `engineering_tickets` row (`UNIQUE(task_id)`);
2. privacy check;
3. ensure labels exist (reused if present);
4. create the issue with body, labels, and assignee;
5. confirm the assignee, record `assignment_confirmed`;
6. add the issue to the Project;
7. set Status = Ready;
8. fill optional Project fields;
9. audit every step.

A ticket that did not finish is stored as `degraded` with the reason, and
Gary's tool output carries a warning so Gary reports the truth rather than
"created and assigned". `retry_incomplete(ticket_id)` finishes it.

### Allowed transitions

```text
Backlog → Ready → In Progress → Review → Security Review → Done
Review → In Progress          Security Review → In Progress
any active state → Blocked    Blocked → Ready | In Progress
```

`Done` is terminal. A ticket with `security_review_required` **cannot** go
Review → Done; it must pass Security Review, and the moment it does is recorded
in `security_reviewed_at`. Dave's automated security review is not connected
yet: nothing in this integration approves on his behalf.

### Issue body

Objective, Requested By, Assigned To, Priority, Project, Requirements,
Acceptance Criteria (checkboxes), Security Requirements, Dependencies,
Estimated Effort, Due, and the Gary Task ID. It is written so Claude Code can
work from it directly. Gary states what is needed and the constraints; Alex
decides implementation. No credentials, customer data, or secret values.

## SQLite schema and synchronization

`004_engineering.sql` adds:

- **`engineering_tickets`**: `id`, `task_id` (unique, FK to `tasks`, cascade
  delete), `github_owner`, `github_repository`, `github_issue_number`,
  `github_issue_node_id`, `github_url`, `github_project_id`,
  `github_project_item_id`, `assigned_to`, `assignment_confirmed`, `priority`,
  `status`, `security_review_required`, `security_reviewed_at`, `sync_state`
  (`pending`, `synced`, `degraded`, `needs_reconciliation`), `sync_error`,
  `created_at`, `updated_at`, `last_synced_at`.
- **`github_project_fields`**: cached, non-secret Project field and option node
  ids, so the board schema is not re-read on every call.

`sync_engineering_ticket` / `sync_all_engineering_tickets` (in code:
`sync_ticket`, `sync_all`) read issue state, Project Status, assignee, and
labels, and adopt GitHub's status. The backend runs `sync_all` every
`GITHUB_SYNC_INTERVAL_MINUTES` (default 30, `0` disables) and Gary can run it
on demand with `engineering_sync`. There is no webhook receiver in v1; the
service boundary is shaped so one can push into `sync_ticket` later.

**Closure rules.** An issue closed while the ticket is Done (and, when
required, has passed security review) completes the Gary task. An issue closed
in any other state is marked `needs_reconciliation`, audited as
`github_sync_failed`, and the company task is **not** completed.

## Gary's tools

`engineering_create_ticket`, `engineering_get_ticket`,
`engineering_list_tickets`, `engineering_mark_ready`,
`engineering_mark_in_progress`, `engineering_mark_review`,
`engineering_mark_security_review`, `engineering_mark_done`,
`engineering_mark_blocked`, `engineering_add_comment`, `engineering_sync`,
`engineering_status`.

There is no raw GitHub request tool, no GraphQL passthrough, and nothing for
repository or organization administration. Comments are for meaningful
operational updates (priority change, moved deadline, new dependency), not
running commentary.

## Audit events

`engineering_ticket_created`, `github_issue_created`,
`github_project_item_added`, `engineering_ticket_assigned`,
`engineering_status_changed`, `engineering_ticket_blocked`,
`engineering_ticket_completed`, `engineering_ticket_commented`,
`github_sync_failed`, `github_privacy_check_failed`. All on entity type
`engineering_ticket`. The token never appears in an audit row.

## Environment variables

| Variable | Meaning |
|---|---|
| `GITHUB_TOKEN` | Fine-grained token (Metadata read, Issues write, Projects write). Empty disables the integration. |
| `GITHUB_OWNER` | Organization or user that owns the private repository and Project |
| `GITHUB_REPOSITORY` | Private repository name |
| `GITHUB_PROJECT_NUMBER` | Private Engineering Project number |
| `GITHUB_ENGINEER_USERNAME` | GitHub username issues are assigned to |
| `GITHUB_SYNC_INTERVAL_MINUTES` | Periodic sync, default 30, `0` to disable |

## Startup and degraded operation

Gary starts normally whether or not GitHub is configured or reachable. When it
is not configured the engineering tools report `unavailable`, and Gary is
instructed never to claim a ticket exists. `GET /engineering/status` returns
`healthy`, `authentication_error`, `permission_error`, `privacy_error`,
`unavailable`, or `misconfigured`, with the privacy audit.

Rate limits: transient failures (429, 5xx, network) get bounded exponential
backoff with jitter, at most three attempts, honoring `Retry-After` up to 60
seconds. Authentication, permission, validation, and privacy failures are never
retried.

## Testing

```bash
./scripts/test.sh                 # whole suite, GitHub faked
docker compose run --rm --no-deps -T -v ./backend:/src:ro -w /src backend \
  sh -c "pip install --quiet --user -r requirements-dev.txt && python -m pytest -q tests/test_engineering.py"
```

`backend/tests/fake_github.py` is an in-memory GitHub (REST + the Projects v2
GraphQL operations used here). No unit test touches the network or creates a
real issue. `backend/tests/test_engineering.py` covers privacy acceptance and
refusal, error mapping, retry behavior, token redaction, creation, labels,
assignment failure, partial creation and idempotent retry, transitions, the
security-review path, synchronization, unexpected closure, and the tool surface.

For an end-to-end check against real GitHub, point `.env` at a **private**
scratch repository and Project, run the setup command, and create one ticket
from a throwaway task.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Refusing to operate because ... is public` | Make the repository or Project private in GitHub yourself. Gary will not. |
| `state: authentication_error` | Token missing, expired, or revoked. Reissue and restart the backend. |
| `state: permission_error` | Token lacks Issues write or Projects write, or is not scoped to this repository. |
| `No Project number N ... is visible` | Wrong `GITHUB_PROJECT_NUMBER`, or the token has no organization Projects access. |
| Ticket stays `degraded` | Read `sync_error`; fix the cause, then retry. It never opens a second issue. |
| `The Project has no 'Ready' Status option` | Add the missing Status options in the Project, then reconcile. |
| Assignment not confirmed | `GITHUB_ENGINEER_USERNAME` cannot be assigned in that repository; give them access. |
| `needs_reconciliation` | An issue closed without satisfying the required state. Decide manually; the task was not completed. |
