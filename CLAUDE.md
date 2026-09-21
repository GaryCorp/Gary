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
python -m app.team_cli team|assign|review|show          # run specialists by hand
python -m app.dry_run [--type management] [--act]       # a planning cycle, observe-only by default
python -m app.costs report|billed|prices|set-price      # what the AI costs
python -m gary.integrations.github.setup                # verify the private GitHub wiring
```

Read-only status endpoints (loopback only): `/health`, `/management/status`,
`/engineering/status`, `/costs`, and the pages `/`, `/team`, `/approvals`,
`/finance`, `/events`.

## Services

`compose.yaml` runs: **backend** (FastAPI, the whole domain), **voice**
(local wake word + speech), **joplin-proxy** (host-loopback bridge to Joplin),
and **ease-api** + **ease-worker** + **ease-redis** (the EASE service Lauren
uses, host port 8002).

Voice is a split pipeline, not an audio model: local faster-whisper detects the
wake word, audio goes to the OpenAI Realtime API with
`output_modalities: ["text"]`, and **local Piper TTS speaks the reply**. This is
why Gary's prompt forbids markdown, lists and symbols.

## Architecture

### Composition root vs domain

`backend/app/` is the composition root and wires concrete integrations into
`gary/`. `main.py` (~2.8k lines) builds the shared objects (`gary_ops`, the
planning cycle, the agents), the web pages, the voice WebSocket, the tool
dispatcher, and the background loops started in `lifespan`. Beside it:
`config.py` (every env setting), `google_auth.py` (encrypted token store,
OAuth flow, which mailbox a call uses), `google_calendar.py`, `gmail.py`,
`joplin.py`, `voice_tools.py` (tool schemas) and `instructions.py` (Gary's
prompt). These import from `config` and each other, never from `main`; tests
patch a name in the module that defines it.

`backend/gary/` is the domain and has no knowledge of FastAPI or Google:

| Package | Role |
|---|---|
| `container.py` | `build_gary()` assembles services over one SQLite database |
| `db/` | numbered SQL migrations + one repository per table; SQL lives here and nowhere else |
| `models/` | pydantic request models; `extra="forbid"`, validated at the boundary |
| `services/` | task/project/action/approval/planning/briefing, the planning cycle, the management loop, engineering tickets |
| `tools/` | the *only* interface the voice model has to SQLite |
| `agents/` | the GaryCorp specialists (CrewAI), their roster, gateway, runner |
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
twice. Finished assignments wake the loop immediately.

### Cost accounting

Every model call is recorded in `model_usage` (planning, specialist, web search,
voice) and priced from `data/model_prices.json`. **An unpriced model is reported
as unpriced, never as free**; EASE runs in its own container and is reported as
unmeasured. With `OPENAI_ADMIN_KEY` (scope `api.usage.read`) Gary also reads the
provider's billed figure.

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
`CONFIGURATION.md` (every `.env` setting), `SETUP.md`, `USAGE.md`,
`TROUBLESHOOTING.md`. Update the relevant one in the same change as the code.
