# GaryCorp Team

Gary manages four specialist AI employees. Each is a separate CrewAI agent
with its own identity, prompt, tools, permissions, context, structured report,
and audit history. Gary decides when their expertise is worth asking for,
delegates, reads their reports, and makes the company-level recommendation.

```text
Gary
├── Susan — Director of Research & Strategy
├── Dave — Director of Security
├── Linda — Director of Operations
└── Catherine — Chief Financial Officer
```

The Ethics Agent is a future department and is not part of this version.

## How work flows

```text
Alex
  ↓
Gary (voice)                     delegate_to_agent / run_management_review
  ↓
AgentService                     roster check, limits, persisted assignment, audit
  ↓
GaryCorpAgentRunner              context package, granted tools, time and output limits
  ↓
CrewAIExecutor                   one single-agent crew per assignment
  ↓
Susan / Dave / Linda / Catherine tool calls go through the ToolGateway
  ↓
Structured report                validated, stored in agent_assignments, audited
  ↓
Gary                             reads reports, compares them, recommends
```

Assignments run in the background, usually one to two minutes each. Gary tells
you who is working on what, you hear an announcement when a report or review
is ready, and Gary reads it on request. To answer "what did Susan find about
X", Gary looks up the report whose objective matches the topic
(`agent_assignment_get` with `about`), so a specialist's different reports are
not confused.

## The employees

| | Susan | Dave | Linda | Catherine |
|---|---|---|---|---|
| Department | Research | Security | Operations | Finance |
| Answers | Options, evidence, tradeoffs, alternatives, what is missing | Attack surface, permissions, trust boundaries, blast radius, controls | Tasks, order, dependencies, estimates, blockers, schedule, deadline realism | Up-front and ongoing costs, budget fit, cheaper alternatives, AI spending, purchases |
| Report | `ResearchReport` | `SecurityReport` | `OperationsReport` | `FinanceReport` |
| Key fields | findings, options, recommendation, assumptions, uncertainties, sources, confidence | risk_level, attack_surfaces, required and recommended controls, recommendation (approve … reject), confidence | proposed_tasks, dependencies, estimated_total_minutes, blockers, deadline_assessment, decisions_needed, confidence | costs (item, amount, frequency), estimated one-time and monthly cost, budget_assessment, savings_opportunities, risks, decisions_needed, recommendation, confidence, purchase_request_ids (set by the application) |
| Tools | `web_search`, `read_project`, `read_tasks`, `read_relevant_notes`, `read_previous_research`, `write_note` | `read_project`, `read_tasks`, `read_agent_permissions`, `read_action_policy`, `read_audit_events`, `read_system_configuration_summary`, `read_relevant_notes`, `write_note` | `read_projects`, `read_project`, `read_tasks`, `read_dependencies`, `read_calendar_availability`, `read_commitments`, `read_followups`, `read_relevant_notes`, `write_note` | `read_finance_status`, `read_purchases`, `read_ai_usage`, `read_projects`, `read_project`, `read_tasks`, `read_relevant_notes`, `web_search`, `request_card_purchase`, `write_note` |
| Joplin notebook | Susan | Dave | Linda | Catherine |
| Context package | assignment, project and tasks, planning notes | assignment, project and tasks, all agents' permissions, action policy, deployment summary, recent security-relevant audit events | assignment, project and tasks, active projects, commitments, follow-ups, calendar availability, planning notes | assignment, project and tasks, card status (brand, last four, expiry), spending limits and this month's committed spend, recent purchase requests, planning notes |

Personalities are deliberately subtle: Susan is curious and evidence-oriented,
Dave skeptical and precise but looking for the safest practical way forward,
Linda practical and unimpressed by unrealistic plans, Catherine careful and
frugal without being stingy. Disagreement comes from
their different jobs, not from scripted conflict.

All definitions live in `backend/gary/agents/roster.py`.

## Permissions

Permissions are enforced in code, not only by prompts:

- **Roster is the authority.** Each agent's `allowed_tools` is defined in
  `roster.py`, as frozen Pydantic models. The `agents` table in SQLite only
  mirrors identity for the org chart; nothing reads permissions from the
  database, and no tool can change the roster.
- **Only granted tools are built.** CrewAI receives wrappers for exactly the
  agent's `allowed_tools`, and each wrapper can only call the gateway.
- **The gateway re-checks every call**: the tool must be granted, arguments
  are validated, per-run limits apply (12 tool calls, 4 web searches), a run
  that timed out cannot make further calls, and every call and denial is
  audited as `agent_tool_called` or `agent_tool_denied`.
- **Specialist tools are read-only except `write_note` and Catherine's
  `request_card_purchase`**, and return filtered data: no credentials, no card
  number, no email content, no audit details, busy calendar time without
  titles, and only Gary's planning notes (never the whole notebook).
- **Notes go only to the agent's own notebook.** `write_note` takes a title
  and Markdown body; the notebook comes from the roster (Susan, Dave, Linda),
  never from the model, and must already exist as a top-level Joplin notebook.
  Agents cannot read, edit, move, or delete notes, or write anywhere else. At
  most 3 notes per assignment; each note ends with who wrote it, when, and for
  which assignment, and each write is audited (long bodies shortened in the
  audit log).
- **Forbidden tools** (sending email, calendar changes, spending money or
  charging a card, reading the card number, permissions, shell, SQL, security
  policy, account creation, deletion, delegation) cannot be granted: a roster
  listing one fails at startup.
- **No delegation by employees.** CrewAI `allow_delegation=False` for all four;
  the roster refuses an employee with `can_delegate`; the service only accepts
  assignments from Gary (or Alex through the CLI).
- **No code execution, memory, or planning** in CrewAI. CrewAI telemetry and
  hosted tracing are off.
- **Reports are validated.** The model fills a plain findings schema; the
  application sets `assignment_id` and validates limits (confidence 0–1,
  enums, list sizes, dependencies naming proposed tasks). Invalid output is
  retried once with the validation error, then recorded as failed.

Employees are advisory: Dave's controls are recommendations, Linda's tasks are
proposals Gary adds only with your agreement, and Catherine's purchases are
requests you approve. Because permissions are per-agent capabilities,
Catherine's purchase authority was added without changing the others.

## Catherine's debit card

Catherine holds GaryCorp's debit card, but she never sees its number and
cannot charge it.

**Giving her the card.** Open `http://localhost:8000/finance` and enter the
card number, expiry, and optionally the name on the card. The security code is
not asked for or stored. The page can also freeze, unfreeze, replace, or remove
the card.

- The full details are encrypted with Fernet into `data/card_vault.enc`
  (owner-only), using `CARD_ENCRYPTION_KEY`, a separate key from the Google
  token key. Without the key, a card cannot be added.
- SQLite (`payment_cards`) holds only brand, last four digits, expiry, and
  status. The audit log records `card_added`, `card_frozen`, `card_unfrozen`,
  and `card_removed` with the last four digits only.
- Nothing reads the vault yet; it is there for a future payment channel.
- Consider a virtual or limited card rather than your main debit card.

**Purchases.** `request_card_purchase` takes a merchant, what it is, the exact
amount in US dollars, the reason, and optionally an `https://` link and
project. It creates a `card_purchase` action:

1. The application checks that the card is active and not expired, the amount
   is within `CFO_PER_PURCHASE_LIMIT_USD` (default $50), and this month's
   committed spend plus the amount is within `CFO_MONTHLY_LIMIT_USD` (default
   $200). Committed means requested and waiting, or approved, in the calendar
   month in `LOCAL_TIMEZONE`; rejected and expired requests do not count.
2. Policy makes it yellow: it waits in `/approvals` with the amount, merchant,
   description, reason, and link.
3. **It can only be approved on the web page.** Gary cannot approve it by voice
   (the service refuses any non-web approval), though he can reject it. Gary
   cannot propose a card purchase himself either; he delegates to Catherine.
4. On approval the limits and card status are checked again, so a frozen card
   or a newer request that used the budget blocks it.
5. **No payment channel is connected yet**, so an approved purchase is
   recorded (`card_purchase_approved`) but the card is not charged; the result
   says so, and the amount stays committed against the month. A payment channel
   will plug in as the action's execute step.

Catherine may make at most 2 purchase requests per assignment, and none during
a management review. Her report's `purchase_request_ids` are filled in by the
application from the run, so she cannot claim a request she did not make.
Gary announces requests when her report is ready. Spending this month and all
requests are on `/finance`.

## Notes

Each specialist keeps notes in their own Joplin notebook: **Susan**, **Dave**,
**Linda**, and **Catherine** (top-level notebooks, next to **Gary**). They write a note when
the assignment asks for one, or for a concise record worth keeping beyond the
report; the structured report is still required. To ask for a write-up:

```text
Gary, have Dave threat-model the approvals page and write up his findings in his notebook.
```

The notebooks must exist; if one is missing, the note is refused rather than
created elsewhere. Gary's own Joplin tools stay limited to the Gary notebook.

## Management reviews

`run_management_review` asks several employees (all four by default, or a
subset) to review one topic **independently**: every first-round assignment
gets the same starting context and none sees another's report. Gary compares
the reports and does not force consensus.

If Gary needs clarification, `management_review_follow_up` allows **one**
targeted follow-up per review, to one employee, optionally sharing named
colleagues' reports. There is no open-ended multi-agent conversation.

A review is `completed` when every first-round report is in, `partial` if some
failed, and `failed` if all did.

## Limits

| Setting | Default | Meaning |
|---|---|---|
| `MAX_AGENT_ITERATIONS` | 8 | CrewAI reasoning/tool iterations per run |
| `MAX_AGENT_EXECUTION_SECONDS` | 300 | Hard time limit per run |
| `MAX_CONCURRENT_AGENT_RUNS` | 2 | Runs executing at once |
| `MAX_ASSIGNMENTS_PER_GARY_PLAN` | 5 | Assignments Gary may create in one conversation or review |
| `CFO_PER_PURCHASE_LIMIT_USD` | 50 | Largest single card purchase Catherine may request |
| `CFO_MONTHLY_LIMIT_USD` | 200 | Card purchases requested or approved per calendar month |
| `MAX_ACTIVE_AGENT_ASSIGNMENTS` | 6 | Queued plus running assignments across the company |

Also fixed in code: one output retry, 12 tool calls, 4 web searches, 3 notes,
and 2 purchase requests per run, one follow-up per review.

## Persistence and audit

Migration `002_agents.sql` adds:

- `agents`: the org chart (id, name, title, department, reports_to).
- `agent_assignments`: who assigned what to whom, status
  (`queued`, `running`, `completed`, `failed`, `cancelled`), context, the
  validated report (`result_json`), and errors.
- `management_reviews`: topic, status, follow-ups used.
- `agent_runs`: per execution, model, attempts, tool calls, token usage, cost
  when the provider reports it, a result summary, and errors.

Migration `003_finance.sql` adds `payment_cards` (brand, last four, expiry,
status; never the number). Purchase requests are `card_purchase` rows in the
existing `actions` and `approvals` tables.

Audit events: `agent_assignment_delegated`, `agent_assignment_started`,
`agent_assignment_completed`, `agent_assignment_failed`,
`agent_assignment_timed_out`, `agent_output_rejected` (with the validation
error), `agent_tool_called`, `agent_tool_denied`,
`management_review_started`, `management_review_completed` / `partial` /
`failed`, and for finance `card_added`, `card_frozen`, `card_unfrozen`,
`card_removed`, `approval_requested` (by `catherine`), and
`card_purchase_approved`. Model reasoning is never stored.

After a restart, running assignments are marked failed (they cannot resume)
and queued ones start again. Reports stay available.

## Asking Gary

```text
Gary, have Susan research the best tools for recording screen demos.
Gary, get Dave's security assessment of the approvals page.
Gary, have Linda create an execution plan for the video project.
Gary, I'm thinking about giving you browser automation. Have your team evaluate it.
Gary, have Research and Security review this independently.
Gary, what did Susan find?
Gary, what is Dave worried about?
Gary, ask Catherine what the team's AI usage is costing us.
Gary, have Catherine buy a USB microphone under forty dollars.
Gary, does Linda think we can finish this Friday?
Gary, show me the management review.
Gary, who's on the team?
```

## Running work by hand

Inside the backend container, the CLI uses the same roster, permissions,
limits, and database, and waits for the reports:

```bash
# The team and recent work
docker compose exec backend python -m app.team_cli team

# One specialist
docker compose exec backend python -m app.team_cli assign susan "Research three ideas for the next experiment"

# Independent management review
docker compose exec backend python -m app.team_cli review "Give GaryCorp a browser automation capability" --agents susan,dave,linda,catherine

# Look up results
docker compose exec backend python -m app.team_cli show <assignment_id>
docker compose exec backend python -m app.team_cli show-review
```

CLI work is audited as assigned by `alex`. The web page
`http://localhost:8000/team` shows the org chart, assignment history, and
reviews.

## Verified behavior

Run against the real models before release:

- "Research three ideas for the next experiment": only Susan ran (84 seconds,
  4 tool calls including web searches) and returned a sourced report that
  separated facts, inference, and uncertainty, and pointed production planning
  to Linda and account access to Dave.
- A three-person review of browser automation (about 2 minutes): Susan
  recommended a staged read-only pilot, Dave rated it high risk and approved
  only with controls (isolated read-only browser, HTTPS allowlist, no logins,
  downloads, or form submission), and Linda proposed a 6-task pilot of about 26
  hours with the deadline marked unknown because none was set. Gary presented
  all three views and recommended a narrow read-only pilot.
- "Have Linda tell me whether a prototype could be finished by next Friday":
  only Linda ran; she flagged the ambiguous date and rated it at risk.
- After a backend restart, all reports were still available to Gary.
- Catherine, asked what the team's AI usage cost over 7 days (no card, no
  purchases): read the usage, verified current model prices by web search, and
  returned a valid `FinanceReport` (about $0.30 for the week, $1.31 a month)
  noting that the provider reported no billed cost. Adding, freezing, and
  removing a card on `/finance` was checked against a throwaway database.

## Models and cost

Specialists use `GARY_EMPLOYEE_MODEL` (default: the planning model) through
CrewAI, with the existing `OPENAI_API_KEY`. Susan's and Catherine's `web_search` uses OpenAI's
hosted web search with `AGENT_WEB_SEARCH_MODEL` (default `gpt-5.4-mini`); each
search is about 15,000 tokens. Token usage per run is stored in `agent_runs`.
