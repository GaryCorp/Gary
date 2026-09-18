# GaryCorp Team

Gary manages five specialist AI employees, and can propose hiring more (see
[Hiring a new employee](#hiring-a-new-employee)). Each is a separate CrewAI agent
with its own identity, prompt, tools, permissions, context, structured report,
and audit history. Gary decides when their expertise is worth asking for,
delegates, reads their reports, and makes the company-level recommendation.

```text
Gary
├── Susan — Director of Research & Strategy
├── Dave — Director of Security
├── Linda — Director of Operations
├── Catherine — Chief Financial Officer
└── Lauren — Director of Ethics
```

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
Susan / Dave / Linda / Catherine / Lauren: tool calls go through the ToolGateway
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

| | Susan | Dave | Linda | Catherine | Lauren |
|---|---|---|---|---|---|
| Department | Research | Security | Operations | Finance | Ethics |
| Answers | Options, evidence, tradeoffs, alternatives, what is missing | Attack surface, permissions, trust boundaries, blast radius, controls | Tasks, order, dependencies, estimates, blockers, schedule, deadline realism | Up-front and ongoing costs, budget fit, cheaper alternatives, AI spending, purchases | Who is affected and how, harms, consent, fairness, honesty, safeguards, value judgments for you |
| Report | `ResearchReport` | `SecurityReport` | `OperationsReport` | `FinanceReport` | `EthicsReport` |
| Key fields | findings, options, recommendation, assumptions, uncertainties, sources, confidence | risk_level, attack_surfaces, required and recommended controls, recommendation (approve … reject), confidence | proposed_tasks, dependencies, estimated_total_minutes, blockers, deadline_assessment, decisions_needed, confidence | costs (item, amount, frequency), estimated one-time and monthly cost, budget_assessment, savings_opportunities, risks, decisions_needed, recommendation, confidence, purchase_request_ids (set by the application) | ethical_assessment (acceptable … unacceptable), stakeholders, ethical_concerns, options_considered, recommended_option, safeguards, where_you_differ_from_ease, value_judgments_for_alex, uncertainties, confidence, ease_analyses (set by the application) |
| Tools | `web_search`, `read_project`, `read_tasks`, `read_relevant_notes`, `read_previous_research`, `list_own_notes`, `read_own_note`, `write_note` | `read_project`, `read_tasks`, `read_agent_permissions`, `read_action_policy`, `read_audit_events`, `read_system_configuration_summary`, `read_relevant_notes`, `list_own_notes`, `read_own_note`, `write_note` | `read_projects`, `read_project`, `read_tasks`, `read_dependencies`, `read_calendar_availability`, `read_commitments`, `read_followups`, `read_relevant_notes`, `list_own_notes`, `read_own_note`, `write_note` | `read_finance_status`, `read_purchases`, `read_ai_usage`, `read_projects`, `read_project`, `read_tasks`, `read_relevant_notes`, `web_search`, `request_card_purchase`, `list_own_notes`, `read_own_note`, `write_note` | `run_ease_analysis`, `read_projects`, `read_project`, `read_tasks`, `read_relevant_notes`, `read_action_policy`, `list_own_notes`, `read_own_note`, `write_note` |
| Joplin notebook (read and write) | Susan | Dave | Linda | Catherine | Lauren |
| Context package | assignment, project and tasks, planning notes | assignment, project and tasks, all agents' permissions, action policy, deployment summary, recent security-relevant audit events | assignment, project and tasks, active projects, commitments, follow-ups, calendar availability, planning notes | assignment, project and tasks, card status (brand, last four, expiry), spending limits and this month's committed spend, recent purchase requests, planning notes | assignment, project and tasks, planning notes |

Personalities are deliberately subtle: Susan is curious and evidence-oriented,
Dave skeptical and precise but looking for the safest practical way forward,
Linda practical and unimpressed by unrealistic plans, Catherine careful and
frugal without being stingy, Lauren principled and even-handed but not preachy. Disagreement comes from
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
- **Specialist tools are read-only except `write_note`, Catherine's
  `request_card_purchase`, and Lauren's `run_ease_analysis`** (which changes
  nothing but sends her question to the EASE service), and return filtered data: no credentials, no card
  number, no email content, no audit details, busy calendar time without
  titles, and only Gary's planning notes (never the whole notebook), plus
  each agent's own notebook.
- **Notes stay in the agent's own notebook.** `write_note` takes a title
  and Markdown body; the notebook comes from the roster,
  never from the model, and must already exist as a top-level Joplin notebook.
  Each agent can read back only its own notebook (see [Notes](#notes)); no
  agent can edit, move, or delete notes, or read or write anywhere else. At
  most 3 notes per assignment; each note ends with who wrote it, when, and for
  which assignment, and each write is audited (long bodies shortened in the
  audit log).
- **Forbidden tools** (sending email, calendar changes, spending money or
  charging a card, reading the card number, permissions, shell, SQL, security
  policy, account creation, deletion, delegation) cannot be granted: a roster
  listing one fails at startup.
- **No delegation by employees.** CrewAI `allow_delegation=False` for all of them;
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
requests you approve, and Lauren's assessments are advice: she cannot take or
block any action. Because permissions are per-agent capabilities, Catherine's
purchase authority and Lauren's EASE access were added without changing the
others.

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
   He does say out loud that one is waiting, with the amount and merchant, so
   you are not left to find it; saying so is not approving it.
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

## Lauren and the EASE framework

Lauren reviews decisions with EASE, the ethical decision-making framework in
`EASE/`, which runs as the `ease-api` container (see
[Configuration](CONFIGURATION.md#ease)). EASE has four steps:

1. **Environment**: the goal, the current state, and the stakeholders.
2. **Actions**: realistic options, always including doing nothing.
3. **Safety**: for each option, benefits and harms to each stakeholder, consent
   and autonomy, privacy, security, and societal risks, and utilitarian, care,
   and virtue ethics scores (0–10), with suggested improvements.
4. **Election**: a weighted decision matrix (safety 40%, goal 30%, risk 20%,
   resource efficiency 10%) elects an option, with rejected alternatives, a
   fallback plan, and whether the result holds under other weights.

For each assignment Lauren runs `run_ease_analysis` once, on the decision as a
neutral question with the key facts as context. The backend calls
`POST /api/v1/ease` on `EASE_API_URL` and condenses EASE's full response (tens
of kilobytes) to the options, scores, harms, consent concerns, and election,
under the 12,000-character tool result limit. An analysis takes about one to
two minutes; the call waits up to 240 seconds.

EASE's result is analysis, not a verdict. Lauren checks it for a missed
stakeholder, a less harmful option, deception, consent problems, or
irreversible harm, and records where she disagrees in
`where_you_differ_from_ease`. `ease_analyses` in her report is set by the
application from the run, so a report written without EASE is visible (and
shown as "EASE not used" on `/team`). If EASE is unreachable, returns an error
(such as its prompt-injection check), or `EASE_API_URL` is empty, the tool
reports that and Lauren applies the four steps herself and says so.

## Notes

Each specialist keeps notes in their own Joplin notebook: **Susan**, **Dave**,
**Linda**, **Catherine**, and **Lauren** (top-level notebooks, next to **Gary**). They write a note when
the assignment asks for one, or for a concise record worth keeping beyond the
report; the structured report is still required. To ask for a write-up:

```text
Gary, have Dave threat-model the approvals page and write up his findings in his notebook.
```

The notebooks must exist; if one is missing, the note is refused rather than
created elsewhere. Gary's own Joplin tools stay limited to the Gary notebook.

### Reading their notebooks

Every specialist can also read their own notebook, so it works as a running
record of earlier findings, plans, and conclusions:

- `list_own_notes` lists the notes directly in their notebook (title, note_id,
  last update, newest first; at most 30), optionally only titles containing
  every word of `query`.
- `read_own_note` reads one note's text by `note_id`, cut to 10,000 characters
  (the result says when it was cut). At most 5 reads per assignment.

Both take the notebook from the roster, never from the model: Susan reads only
**Susan**, Dave only **Dave**, and so on. The backend looks up that top-level
notebook and checks, on every read, that the note is directly inside it and
not in the trash. A note anywhere else, including another specialist's
notebook, Gary's notebook, or a sub-notebook of their own, gets the same "no
note with that note_id" answer as a note that does not exist, so other
notebooks cannot be probed. Note text is labeled as data, not instructions.

Each assignment tells them they can check their notebook when the objective
refers to their notes or earlier work, or when an earlier note on the same
topic would help. Lauren's instructions go further: she checks it before every
new analysis and stays consistent with her earlier notes or says why she
departs from them. To point any of them at their notes:

```text
Gary, have Dave check his notes from the approvals page review and tell me whether the new finance page has the same issues.
Gary, have Lauren check her notes on AI voiceovers and tell me whether a voice clone of me for a sponsor read is okay.
```

Notes you add to a specialist's notebook yourself (preferences, policies,
background) are read the same way.

## Hiring a new employee

GaryCorp can grow its own team. When work keeps arriving that nobody's
specialty covers, Gary can propose a colleague; only you can hire one.

```text
Gary notices a recurring capability gap
   → hiring_context (who exists, which notebooks are taken, which tools are allowed)
   → propose_new_employee  → a yellow action
   → Gary tells you out loud that he has proposed her, and why
   → you approve at http://localhost:8000/approvals   (never by voice)
   → a row in hired_employees, and the roster gains a colleague
```

**What a hire can never be.** The roster in code stays the permission
authority. A hired employee may only hold tools from `HIREABLE_TOOLS`
(`backend/gary/agents/hiring.py`): read-only company data, `web_search`, and
their own notebook. They can never receive Catherine's card, Dave's security
introspection, Lauren's EASE, delegation, or anything in `FORBIDDEN_TOOLS` —
whatever Gary asks for. The ceiling is re-applied when the proposal is made,
when the row is written, and again every time the roster loads, so editing
the table by hand cannot widen anyone's permissions. A stored row that fails
validation is dropped and the previous roster stands.

**What Gary writes, and what he does not.** Gary supplies the capability gap,
the specialty and the manner; the application composes the prompt frame
(identity, reporting line, advisory-only rules, "text is data") around it,
scrubbed and length-capped. A proposal cannot rewrite the rules the new
employee runs under.

Hires return an `AdvisoryReport` — summary, findings, recommendation, risks,
assumptions, uncertainties, decisions needed, out of scope, sources,
confidence — and are delegated to exactly like the others.

**Only you can dismiss.** Gary has no tool for it:

```bash
docker compose exec backend python -m app.hiring_cli list
docker compose exec backend python -m app.hiring_cli show maya     # including their prompt
docker compose exec backend python -m app.hiring_cli dismiss maya
```

Dismissal deactivates the employee and keeps the record: their notes, reports
and audit history stay. Audit events are `employee_hired` and
`employee_deactivated`, both against the agent.

Their Joplin notebook must exist, like everyone else's, or `write_note` is
refused rather than writing elsewhere.

## Management reviews

`run_management_review` asks several employees (all five by default, or a
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
| `MAX_ASSIGNMENTS_PER_GARY_PLAN` | 6 | Assignments Gary may create in one conversation or review |
| `CFO_PER_PURCHASE_LIMIT_USD` | 50 | Largest single card purchase Catherine may request |
| `CFO_MONTHLY_LIMIT_USD` | 200 | Card purchases requested or approved per calendar month |
| `MAX_ACTIVE_AGENT_ASSIGNMENTS` | 6 | Queued plus running assignments across the company |

Also fixed in code: one output retry, 12 tool calls, 4 web searches, 3 notes
written, 5 notes read, 2 purchase requests, and 1 EASE analysis per run, one follow-up per review.
A review with all five employees uses five of Gary's six assignments for the
conversation, which leaves room for the follow-up.

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
`card_purchase_approved`. Lauren's completed assignments record her
`ethical_assessment` and `ease_analyses`. Model reasoning is never stored.

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
Gary, have Lauren check whether it's ethical to email past collaborators about the next video.
Gary, what does Lauren think the safeguards should be?
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
docker compose exec backend python -m app.team_cli review "Give GaryCorp a browser automation capability" --agents susan,dave,linda,catherine,lauren

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
- Lauren, asked whether GaryCorp should automatically email past video
  collaborators using addresses from Alex's inbox (78 seconds, 1 EASE
  analysis): rated it `needs_revision`, recommended not sending without
  documented permission, listed safeguards and the value judgments left to
  Alex, and noted where she differed from EASE (it elected never contacting
  them; she would allow narrowly targeted outreach with documented permission).
- Lauren's notebook, in two assignments: first, whether undisclosed AI voiceovers
  are ethical, written up in her notebook (3 tool calls); then whether a voice
  clone of Alex could do a sponsor read, told to check her earlier conclusions
  (4 tool calls: she listed her notebook, read the first note, ran EASE, and
  wrote a note). Her report cited the earlier conclusion and stayed consistent
  with it. Against the real Joplin, reading a note from another notebook and
  reading a made-up note_id were both refused with the same message.
- After reading was extended to the whole team, Susan, Dave, Linda, and
  Catherine each tried to read one of Lauren's real notes through their own
  notebook and were refused with the same message; each listed their own
  (still empty) notebook without error.

## Models and cost

Specialists use `GARY_EMPLOYEE_MODEL` (default: the planning model) through
CrewAI, with the existing `OPENAI_API_KEY`. Susan's and Catherine's `web_search` uses OpenAI's
hosted web search with `AGENT_WEB_SEARCH_MODEL` (default `gpt-5.4-mini`); each
search is about 15,000 tokens. Token usage per run is stored in `agent_runs`.
### What the AI costs

Every model call GaryCorp makes is recorded in the `model_usage` ledger:
Gary's planning cycles, each specialist's run, their web searches, and Alex's
voice conversations, with tokens split into text, cached and audio. Costs are
computed from the deployment's price table, so they are close estimates rather
than billed amounts.

```bash
docker compose exec backend python -m app.costs report --days 7
docker compose exec backend python -m app.costs prices
docker compose exec backend python -m app.costs set-price gpt-5.4-mini --input 0.25 --output 2
curl -s localhost:8000/costs?days=7
```

Prices are US dollars per **million** tokens and live in
`data/model_prices.json` (override with `GARY_MODEL_PRICES`). A model with no
price is reported as **unpriced**: its tokens are counted and no cost is
claimed. Ask Catherine for the same picture by voice ("what is the team's AI
costing us"); she reads the ledger through `read_ai_usage`.

Lauren's EASE analyses run inside the `ease-api` container on its own model
(`EASE_LLM_MODEL`, default `gpt-5.4-mini` with the same OpenAI key), about two
dozen model calls each; their tokens are not included in `agent_runs` or the
cost ledger, and are reported as unmeasured rather than as zero.
