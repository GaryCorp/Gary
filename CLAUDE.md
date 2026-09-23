# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Gary is a local voice assistant and AI "Chief of Staff" that runs GaryCorp: a
small company of AI employees managing Alex's calendar, email, notes, projects,
money and engineering work. Everything runs in Docker on one machine.

`EASE/` is a separate FastAPI service (an ethical decision-making framework)
with its own `CLAUDE.md`.

## Commands

```bash
make setup          # ./setup.sh: generates .env with fresh secrets and UIDs
make up             # start; make down, make logs, make status, make rebuild
make test           # the whole backend suite, in the backend image
```

Run one test (the suite always runs inside the image, never on the host —
dependencies are not installed locally):

```bash
docker compose run --rm --no-deps -T -v ./backend:/src:ro -w /src \
  -e PYTHONDONTWRITEBYTECODE=1 backend \
  sh -c "pip install --quiet --user -r requirements-dev.txt && \
         python -m pytest -p no:cacheprovider -q tests/test_agents.py::test_registry_loads_five_employees_and_gary"
```

**After changing backend code, rebuild the image** — `docker compose up -d
backend` alone silently keeps the old code, and a failed build (transient
registry DNS errors happen here) leaves the previous image running:

```bash
docker compose build backend && docker compose up -d backend
```

Operator CLIs, all run inside the backend container:

```bash
python -m app.ask [--show-tools] "..."                  # talk to Gary in text (company tools only)
python -m app.team_cli team|assign|review|show          # run specialists by hand
python -m app.hiring_cli list|show|dismiss              # hired employees; dismissal is Alex's alone
python -m app.dry_run [--type management] [--act]       # a planning cycle, observe-only by default
python -m app.costs report|models|billed|prices|set-price     # what the AI costs
python -m gary.integrations.github.setup                # verify the private GitHub wiring
```

Read-only status endpoints (loopback only): `/health`, `/management/status`,
`/engineering/status`, `/production/status`, `/costs`, and the pages `/`, `/team`, `/approvals`,
`/finance`, `/events`.

## Services

`compose.yaml` runs: **backend** (FastAPI, the whole domain), **voice**
(local wake word + speech), **joplin-proxy** (host-loopback bridge to Joplin),
and **ease-api** + **ease-worker** + **ease-redis** (the EASE service Lauren
uses, host port 8002).

Voice is a split pipeline, not an audio model: local faster-whisper detects the
wake word and segments the utterance, and **local Piper TTS speaks the reply**.
In between, `VOICE_MODE` picks one of two paths:

- `transcribe` (default, `app/voice_turn.py`): the utterance goes to a
  transcription model (billed per minute), the words go to a text model over the
  Responses API with Gary's tools. History is kept in the backend because the
  text model is stateless.
- `realtime`: the original single OpenAI Realtime session with
  `output_modalities: ["text"]`; `app/realtime.py` serialises response
  turn-taking so a tool result is never dropped.

Both use the same tool schemas. Piper speaking is why Gary's prompt forbids
markdown, lists and symbols.

## Architecture

### Composition root vs domain

`backend/app/` is the composition root and wires concrete integrations into
`gary/`. `main.py` builds the shared objects (`gary_ops`, the planning cycle,
the agents, the cost ledger and spend gate), the status endpoints, the voice
WebSocket, and the background loops started in `lifespan`. Beside it:

- `config.py` (every env setting), `google_auth.py` (encrypted token store,
  OAuth flow, which mailbox a call uses), `google_calendar.py`, `gmail.py`,
  `joplin.py` — the integrations;
- `pages.py` — the HTML pages and sign-in routes, an `APIRouter` that reads
  `gary_ops`, `agent_service` and `card_vault` from `request.app.state`;
- `calendar_actions.py` (Google-backed action handlers), `notebooks.py`
  (Joplin notebooks used outside a conversation), `tool_dispatch.py`
  (Google/Joplin voice tools), `announcements.py` (what Gary says unprompted),
  `system_summary.py`, `local_time.py`;
- `voice_tools.py` (tool schemas), `instructions.py` (Gary's prompt),
  `voice_turn.py` / `realtime.py` + `realtime_session.py` (the two voice
  paths), and the operator CLIs.

These import from `config` and each other, **never from `main`** — anything
that needs a runtime object takes it as an argument or from `app.state`. The
CLIs are the exception: they import `main` to reach the live objects. Tests
patch a name in the module that defines it (e.g. `pages.make_flow`).

`backend/gary/` is the domain and has no knowledge of FastAPI or Google:

| Package | Role |
|---|---|
| `container.py` | `build_gary()` assembles services over one SQLite database |
| `db/` | numbered SQL migrations + one repository per table; SQL lives here and nowhere else |
| `models/` | pydantic request models; `extra="forbid"`, validated at the boundary |
| `services/` | task/project/action/approval/planning/briefing, follow-ups and commitments, the planning cycle, the management loop, engineering tickets, spoken messages (`conversation_service`), hiring and reorg proposals, performance facts, the weekly review |
| `tools/` | the *only* interface the voice model has to SQLite |
| `agents/` | the GaryCorp specialists (CrewAI), their roster, gateway, runner, hiring (`HIREABLE_TOOLS`, prompt frame), EASE client |
| `finance/` | card vault, purchase policy, model prices, usage ledger |
| `integrations/github/` | private-only engineering tickets (REST + Projects v2) |

### The rules that hold the system together

These are enforced in code and are the reason the design looks the way it does.
Breaking one is almost always a bug.

- **The model proposes, Python decides.** The planner returns a strict JSON
  schema; `planning_cycle.validate_cycle_actions` re-checks every proposal
  deterministically and applies caps. Specialists return structured findings
  that the runner validates before storage.
- **Every side effect is an action.** `ActionService.propose` looks the type up
  in `policy.py` (green runs, yellow waits for approval, red is refused), then:
  validate → run the external effect **with no transaction open** → record the
  outcome → audit. A handler may escalate risk, never lower it. A failure is
  recorded as a failure, never as success.
- **Permissions are data in code, re-checked at the boundary.**
  `agents/roster.py` is the authority for what each specialist may do; the
  `agents` table only mirrors identity. `ToolGateway` re-checks every call
  against the roster, applies per-run caps and audits calls and denials.
  `FORBIDDEN_TOOLS` cannot be granted at all.
- **Specialists are advisory.** A report changes nothing by itself. Only Gary
  delegates; employees cannot.
- **Hiring is an argument, not an act.** Gary may propose a colleague in
  conversation or in a cycle (capped like a reorg: one per cycle, none while
  one is pending, 14 quiet days). Approval files a ticket whose objective
  cites counted evidence; `engineering_extend_spec` may append to that issue
  later; and once the colleague exists `hiring_followup.py` comments their
  record back onto it, once, with no model call.
- **The company's shape is a code change.** `sync_roster` mirrors `roster.py`
  into the database on every start. Hiring and reorganisation are yellow
  actions: approving one files a private engineering ticket for Alex to edit
  `roster.py` and deploy; it never creates an agent or changes a permission by
  itself. Gary cannot be the subject of a reorg. Employees hired under the
  older data-driven flow (`hired_employees`) still load, but only if they
  validate against `HIREABLE_TOOLS`.
- **Speaking first is an action.** Deciding to say something writes a
  `spoken_messages` row (validated, capped, never a duplicate open question);
  delivery is separate and retried, and a row is marked spoken only once a voice
  client received it.
- **SQLite is the source of truth** for company state; GitHub owns the external
  workflow state of an issue. `engineering_tickets.task_id` is unique, which is
  what makes ticket creation idempotent.
- **Privacy fails closed.** The GitHub integration verifies the repository and
  Project are private and owned by `GITHUB_OWNER` before every write, treats an
  unreachable GitHub as unsafe, and has no code to change visibility.
- **Timestamps** are ISO 8601 UTC strings with an offset (`timeutil.py`), so
  string comparison works in SQL; convert to local only for display.
- **Migrations** are numbered SQL files applied in order, each in its own
  transaction. SQLite cannot alter a CHECK constraint, so changing an enum means
  rebuilding the table (see `005_management_cycles.sql`).
- **Secrets live in the environment only** — never in SQLite, Joplin, prompts,
  issue bodies, or logs. Authorization headers are redacted; issue text is
  scrubbed of anything token-shaped.

### The autonomous loop

Gary runs the company between conversations:

```text
scheduled cycles (08:00/12:30/17:30)  +  management loop (every 15 min)
        ↓                                        ↓
  one model call per cycle              cheap SQLite trigger check first:
        ↓                               new reports? missed block? follow-up
  proposals validated in Python         due? approval expiring? → else skip,
        ↓                               costing nothing
  actions via the policy pipeline
```

A cycle may create tasks, delegate to a specialist, or start a management
review, under caps in `planning_cycle.py` (2 delegations per cycle, 4 per day,
1 review per day) plus repeat detection so the same question is not commissioned
twice. Finished assignments wake the loop immediately. The management loop also
has a daily run ceiling.

Other background loops started in `lifespan`: spoken-message delivery, the
GitHub ticket sync, the missed-block replan, new-email announcements, the daily
SQLite backup (`gary/backup.py`, to `data/backups/`, 14 kept), and the weekly
review (`WEEKLY_REVIEW_DAY`), written to Joplin from SQLite with **no model
call** so it still works when spending is stopped.

The weekly video schedule (`services/production.py`, on when
`PRODUCTION_FIRST_SHOOT` is set) is the same kind of job: once a day, with no
model call, it plans each batch of episodes two weeks before its shoot as
ordinary projects and tasks with deadlines worked back from the publish time
(`production_episodes.episode_number` is the idempotency key), and schedules
the fixed-time shoot and publish slots through `schedule_task`. The planner
fits the flexible work around them. Tasks in a `planned` project are never
ready (`readiness.py`), which is how work is parked without deleting it. Each
stage is also a private GitHub issue of kind `production`
(`engineering_tickets.kind`): closing the issue completes the stage and
completing the stage closes the issue, with no Review step.

`services/operating.py` is the company's on/off switch: `operating_state`
holds paused and the cursor into Alex's command emails, so both survive a
restart. Alex mails `pause` or `resume` to Gary's own mailbox; a command is
acted on only if it is from his exact address, Gmail's `Authentication-Results`
passes SPF or DKIM for that domain, the word is the whole subject or first
line, and the message is newer than the cursor. Pausing stops every unattended
loop in `main.py`; voice and the web pages keep working. Email can do nothing
else — approvals stay on the web page. `services/daily_report.py` emails Alex
the day (no model call) at `DAILY_REPORT_TIME`.

Gary also manages Alex directly (`services/accountability.py`, no model
call): a morning assignment spoken and emailed through `email_principal` (a
green action with no recipient field), an evening check-in on what is not
done, and Alex's totals in the weekly review. Both are recorded in the audit
log keyed by date, which is what keeps each to once a day.

### Cost accounting

Every model call is recorded in `model_usage` (planning, specialist, web search,
voice) and priced from `data/model_prices.json`. **An unpriced model is reported
as unpriced, never as free**; EASE runs in its own container and is reported as
unmeasured. With `OPENAI_ADMIN_KEY` (scope `api.usage.read`) Gary also reads the
provider's billed figure.

`MAX_DAILY_AI_SPEND_USD` is a hard daily ceiling: once reached, everything that
calls a model stops until local midnight, voice included (`spend_stop` in
`main.py`). It can only be enforced for priced models, so the gate reports
itself unenforceable rather than "within budget" when a call is unpriced, and
`REQUIRE_PRICED_MODELS` (default on) refuses unattended work while any
*configured* model has no price.

## Tests

`backend/tests/` runs against temporary SQLite files with fakes for every
external system — Google, Joplin, OpenAI, CrewAI (`FakeExecutor`), GitHub
(`fake_github.py`). **No test may touch the network or create a real issue,
event or email.** Shared fixtures are in `conftest.py`: `gary`, `clock`
(`FakeClock`), `external`, `db_path`, plus `make_project` / `make_task`.

When adding behaviour, the existing suites expect: audit events asserted by
name and order, caps and refusals tested as much as happy paths, and integration
fakes that reproduce the real API's *failure* shapes (GitHub's partial GraphQL
errors and 404-for-private-resources both caused real bugs that passing tests
had missed).

## Documentation

`docs/` is written for the operator and is kept current with the code:
`ARCHITECTURE.md`, `TEAM.md` (the five specialists, permissions, EASE, costs),
`ENGINEERING.md` (GitHub tickets), `SECURITY.md`, `PRIVACY.md`,
`CONFIGURATION.md` (every `.env` setting), `GOOGLE_OAUTH.md` (both mailboxes),
`SETUP.md`, `USAGE.md`, `TROUBLESHOOTING.md` (including restoring a backup),
`PROJECT_FILES.md` (what each file is for). Update the relevant one in the same change as the code.
