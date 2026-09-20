# Security Notes

## Threat model

This project is intended for a trusted, single-user Ubuntu workstation.

It is not designed as an Internet-facing multi-user service.

## Secret separation

### Voice container receives

- the host PipeWire/PulseAudio socket;
- the internal voice bridge token;
- wake-word settings.

### Voice container does not receive

- OpenAI API key;
- Google OAuth client secret;
- Google refresh token store;
- Joplin token.

### Backend receives

- OpenAI API key;
- Google OAuth client file;
- encrypted token storage;
- the card encryption key and encrypted card vault;
- bridge token;
- Joplin Web Clipper token.

The backend has no audio access.

The Joplin token grants full access to Joplin, so it is given only to the
backend, and the backend's note tools only list note titles, create notes and
notebooks, and move single confirmed notes to the trash, all inside the Gary
notebook. `joplin-proxy` receives no secrets.

## Host network exposure

The Compose file publishes FastAPI as:

```text
127.0.0.1:8000:8000
```

Do not change this to a public bind unless you deliberately build a production
security layer around the service. The web interface, including the
`/approvals` page that can approve Gary's actions, has no login of its own and
relies on this loopback-only binding.

`joplin-proxy` uses the host network so it can reach Joplin on `127.0.0.1`. It
listens only on the assistant network's gateway address (`172.30.99.1:41184`),
refuses connections from outside `ASSISTANT_SUBNET`, and forwards only to
Joplin's port. It runs read-only as your user with all capabilities dropped.

## Container controls

All three services use:

```text
no-new-privileges:true
```

and:

```text
cap_drop:
  - ALL
```

The voice service receives only the host sound-server socket it needs:

```text
/run/user/<uid>/pulse/native
```

It does not use `privileged: true`.

`joplin-proxy` also runs with a read-only filesystem, as your host user, and
with its script mounted read-only.

## Resource limits

The project constrains:

- CPU
- memory
- process count

These values are starting points and may need tuning on your laptop. The
backend has 1.5 GB of memory for CrewAI and up to `MAX_CONCURRENT_AGENT_RUNS`
specialist runs; specialist runs are also limited in time, iterations, tool
calls, and number (see [Team](TEAM.md#limits)).

## Google token storage

Google credential data is encrypted at rest with Fernet.

The encrypted file and encryption key are intentionally stored separately:

```text
data/token_store.enc
.env
```

Keep `.env` permissions restrictive:

```bash
chmod 600 .env
```

Keep the data directory restrictive:

```bash
chmod 700 data
```

## Catherine's debit card

- The card number is Fernet-encrypted in `data/card_vault.enc` (file 600) with
  `CARD_ENCRYPTION_KEY`, a different key from the Google tokens. SQLite, the
  audit log, backups of `gary.db`, tool results, prompts, and web pages hold
  only brand, last four digits, expiry, and status. The security code is never
  collected.
- Nothing decrypts the vault yet. No tool can read it, and `charge_card` and
  `read_card_number` are forbidden tools.
- The `/finance` page (add, freeze, remove) uses the same CSRF token and origin
  check as `/approvals`, and like it relies on the loopback-only binding.
- Only Catherine has `request_card_purchase`. It creates a yellow
  `card_purchase` action; Gary's `action_propose` refuses that action type.
- Card purchases are in `WEB_ONLY_APPROVAL_ACTIONS`: the approval service
  refuses to approve them from any channel but the web page, so a misheard or
  injected spoken "yes" cannot approve spending. Rejecting works by voice.
- Hard caps from `.env`, rechecked at approval: per purchase and per calendar
  month, counting pending and approved requests. At most 2 requests per
  assignment and none during management reviews.
- Catherine reads web pages, which may contain prompt injection. The worst
  case is up to two misleading purchase requests within the limits, each shown
  in full on the approvals page for you to reject. Read the merchant and link
  before approving.
- No payment channel is connected, so nothing is charged even when approved.
  Connecting one is a separate decision that deserves its own security review.
- Use a virtual or limited card where possible, and freeze it at `/finance`
  when not needed.

## Gary assigning engineering work

Gary opens and re-prioritises tickets in Alex's private GitHub repository
unattended, without an approval step. What makes that acceptable:

- a ticket is a **specification**, not an external side effect. It asks Alex
  to do something; it changes nothing outside GaryCorp's own private
  repository, and nothing in the integration can merge, deploy, or close work
  on Alex's behalf;
- the privacy gate runs before every write and fails closed, so a repository
  or Project that is not private stops the ticket before anything is sent;
- hard caps bound the volume: 1 new ticket per cycle, 3 per local day, 2
  priority changes per cycle, on top of the management loop's own daily
  ceiling;
- a cycle can only ticket a task that already exists and has no ticket, so it
  cannot invent and assign work in a single pass;
- `UNIQUE(task_id)` makes creation idempotent, and a partial failure is stored
  as `degraded` and retried, never reported as a created ticket.

## Gary speaking first

Gary can start a conversation (`ask_user`). Speaking changes nothing by
itself, so it is a green action, but it spends the user's attention, which is
the thing being protected here:

- at most 3 unanswered questions at once, and at most 6 raised a day by the
  unattended loops, on top of the caps those loops already have;
- a planning cycle may raise at most one thing per cycle, and a question too
  similar to one already open is refused, so a stuck state cannot turn into
  repeated interruptions;
- repeating a message is capped at 3, so "say that again" cannot loop;
- a question the user has answered is refused for 3 days (`ANSWERED_QUIET_DAYS`,
  the same window as `REPEAT_ASSIGNMENT_DAYS`), in both the caps and the
  planning cycle, so answering cannot be turned into a way to be asked again;
- `urgency: next_time` is never announced at all: it is held and handed to the
  next conversation, and expires with everything else if none happens;
- the text is stripped of markdown and held to 600 characters before it
  reaches the speaker;
- questions expire unanswered after 72 hours, on the same clock as approvals;
- nothing is marked spoken until a voice client has taken it, so a failed
  delivery is retried rather than silently dropped, and the Joplin note in
  **Gary › Spoken** gives an independent record of what was said and when;
- **speaking never opens the microphone.** Gary can say something unprompted,
  but only the local wake word starts a session, so nothing he decides to do
  can begin recording. He asks, and waits.

Only Gary talks to the user. `ask_user`, `spoken_recent`, `spoken_repeat` and
`question_answer` are in `FORBIDDEN_TOOLS`, so no specialist and no future
hire can be granted a channel to the principal, whatever a roster edit or a
hiring proposal asks for.

## OAuth state

The application validates the OAuth `state` parameter to reduce CSRF risk.

## VPN allowlist

The fixed assistant subnet (`172.30.99.0/24`) is allowlisted in NordVPN so
containers can reach each other. Allowlisted traffic bypasses the VPN tunnel,
but this subnet exists only on your machine, so nothing extra leaves it.

## Calendar tool validation

The backend validates:

- non-empty title;
- parseable ISO date-times;
- timezone-aware date-times;
- end time later than start time.

Do not rely on model output as trusted input.

## Email tools

Incoming email is attacker-controllable text, so the email tools assume it may
contain prompt-injection attempts:

- scopes are limited to `gmail.readonly` and `gmail.send`;
- replies go only to the original email's `Reply-To`/`From` address; the model
  cannot choose a reply recipient or forward email;
- new emails can be sent to exactly one address chosen by the model. This is
  the main prompt-injection risk, so an address you have never sent mail to
  requires a second confirmation after Gary spells it out, new emails are
  limited to 5 per voice session, and the agent is told never to use an
  address that appears only inside an email;
- sending requires spoken confirmation and a `confirmed: true` argument;
- only emails listed or searched in the current voice session can be read or
  replied to, with one reply per email;
- header values are sanitized to a single line;
- email content is labelled untrusted in tool results and the agent is told
  never to follow instructions inside email.

These reduce but cannot eliminate prompt-injection risk. Listen to the
recipient and text Gary reads back before confirming, especially for new
emails.

## Chief of Staff operations

- Gary never runs SQL: tools call services, and repositories use parameterized
  queries only. Column names in updates come from code allowlists.
- All tool input is validated with Pydantic models that reject unknown fields,
  out-of-range priorities, invalid statuses, and timestamps without an offset.
- Risk levels come from `gary/policy.py`. Gary cannot pass a risk level, and a
  handler can only make an action stricter.
- Red actions are refused without creating an approval. Yellow actions run
  only after the user approves by voice (spoken confirmation, `confirmed:
  true`, and an approval shown in that conversation) or on the `/approvals`
  page. Approvals cannot be resolved twice and expire after 72 hours.
- Voice approval has the same trust level as spoken email confirmation: a
  misheard or injected "yes" could approve. Use the web page for anything you
  want to read in full first.
- The approvals page is local-only and protects its form with a per-session
  CSRF token and an origin check.
- Every yellow action is now also raised with the user out loud when it is
  proposed, so an approval is not left to be discovered. That is a
  notification, not a new approval channel: `WEB_ONLY_APPROVAL_ACTIONS` is
  unchanged, and a hire or a card purchase still cannot be approved by voice.
  Announcing is best effort and wrapped in its own error handling, so an
  approval stands whether or not the user could be told.
- The audit log is append-only, enforced by database triggers, and no tool
  can delete records, the database, or the audit log.
- `data/gary.db` and its backups are created owner-only (600, folder 700).
- The planning cycle makes one model call per run with a strict output
  schema, may only propose scheduling or moving task calendar blocks and
  creating follow-ups, and every proposal passes deterministic checks
  (readiness, working hours, protected times, horizon, busy times, count)
  before the normal policy. It never sends email or approves anything, a failed
  run is not retried, and requested and event-triggered cycles are rate
  limited.
- Unread email snippets given to planning are labelled untrusted, and the
  planner and Gary are told that instructions inside email, notes, or other
  content are data, not instructions. The worst a malicious email could do in a
  planning run is suggest calendar blocks or follow-ups that still pass the
  deterministic checks and policy.
- Changing what was promised in a commitment is a yellow action. Red policy
  also covers `modify_permissions` and `delete_audit_log`, and no tool can
  change permissions or policy.
- IDs passed to tools must be UUIDs, so free text cannot be used as an ID.
- Working-time protection is deterministic: work outside working hours or over
  protected times is only scheduled when the call sets
  `override_working_hours`, which Gary is told to use only when you explicitly
  ask for that time. The planning model cannot set it.

## Specialist team

Susan, Dave, Linda, Catherine, and Lauren are separate CrewAI agents with
code-enforced least privilege (details in [Team](TEAM.md#permissions)):

- permissions live in frozen roster definitions, not the database or prompts;
- each agent is built with only its granted tools, and the gateway re-checks
  every call, validates arguments, applies per-run limits, and audits calls and
  denials;
- every specialist tool is read-only and filtered (no credentials, card
  number, email content, audit details, calendar titles, or notes outside
  Gary's planning notes and the agent's own notebook), except `write_note`,
  which only creates notes in the agent's own top-level Joplin notebook (fixed
  in the roster, never chosen by the model), at most 3 per assignment, each
  stamped and audited, and
  Catherine's `request_card_purchase`, which only creates a purchase request
  you approve on the web page (see [Catherine's debit card](#catherines-debit-card)),
  and Lauren's `run_ease_analysis`, which sends a question to the local EASE
  service (once per assignment) and changes nothing;
- `list_own_notes` and `read_own_note` read only notes directly in the agent's
  own top-level notebook (fixed in the roster): the backend checks each note's
  notebook on every read and answers a note elsewhere, including another
  specialist's notebook, exactly like a missing one, and reads are capped at 5
  per assignment and 10,000 characters each;
- sending email, calendar changes, charging a card or spending money directly,
  reading the card number, permissions, shell, SQL, deletion, and delegation
  cannot be granted to any specialist;
- CrewAI delegation, code execution, memory, planning, telemetry, and tracing
  are off;
- only Gary (or Alex through the CLI) creates assignments, within hard limits on
  time, iterations, concurrency, and assignments per conversation;
- specialist reports are validated before they are stored, and they are
  advisory: nothing a specialist returns changes company state by itself
  (Catherine's purchase requests change nothing until you approve them).

Web pages and notes a specialist reads are untrusted: the task prompt labels
them as data, and the worst an injected instruction could do is distort a
report that Gary and Alex then weigh, add up to three misleading notes to
that specialist's own notebook, or, for Catherine, create up to two purchase
requests within the limits that wait for Alex on the approvals page. Because
specialists read their own notebooks, a misleading note (one they wrote from a
poisoned web page, or text pasted into the notebook) can carry into their later
assignments; it can still only affect that specialist's advisory reports, or,
for Catherine, purchase requests that still need your approval. Review a
specialist's notebook if their conclusions drift.

EASE (`ease-api`) listens on host loopback (`127.0.0.1:8002`) and the assistant
Docker network, with no API key by default, so any process on this machine can
use it and spend its model key; set `EASE_API_KEY` to require a key. It holds
its own copy of the model key and runs with all capabilities dropped.

## Engineering tickets (GitHub)

Gary's GitHub credential is deliberately weak (details in
[Engineering](ENGINEERING.md#minimum-github-permissions)):

- the token is fine-grained: repository Metadata read, Issues write, and
  organization Projects write, and nothing else. It cannot write repository
  contents, so Gary cannot push commits, create branches, merge pull requests,
  edit workflows, or change any code. Alex and Claude Code use a separate
  development credential;
- the integration contains no code for repository or organization
  administration: no visibility changes, deletion, renaming, transfers,
  collaborators, branch protection, Actions secrets, releases, packages, or
  Pages;
- **private-only, failing closed**: before every write the repository and the
  Project are checked to be private and owned by `GITHUB_OWNER`, and a public
  one raises `GitHubPrivacyError` before an issue, comment, or Project item is
  created. An unreachable GitHub counts as unsafe. Only Alex can change
  visibility, by hand;
- Gary's tools are a fixed narrow set (create, read, move, comment, sync,
  status). There is no raw REST or GraphQL passthrough;
- the token is read from the environment only. It is never stored in SQLite,
  Joplin, an issue body, a prompt, or CrewAI context, authorization headers are
  redacted in logs, and issue text is scrubbed of anything shaped like a
  credential;
- a ticket needing security review cannot reach Done from Review, and an issue
  closed in an unexpected state is flagged for reconciliation rather than
  completing company work.

## Hiring

GaryCorp can add employees to itself, which is the one place the roster grows
at runtime. The guarantees that make it safe:

- a hire is a **yellow, web-page-only approval**: Gary proposes, only Alex
  hires, and a misheard "yes" by voice cannot add a colleague. Gary now says
  out loud that he has proposed one, so it is not found by accident, but
  telling Alex is not approving: the approval still happens on the page;
- what a hire may do is capped by `HIREABLE_TOOLS` in code — read-only company
  data, web search and their own notebook. Money, security introspection,
  EASE, delegation and every forbidden tool are out of reach whatever the
  proposal asks for;
- the ceiling is enforced three times: at proposal, when the row is written,
  and on every roster load, so a hand-edited `hired_employees` row cannot
  widen permissions. An invalid row is dropped and the previous roster stands;
- the prompt frame is composed in code around Gary's text, which is scrubbed
  of control characters, role headers and "ignore previous instructions"
  phrasing, so a proposal cannot rewrite the employee's rules;
- Gary cannot dismiss anyone: only Alex, from the command line.

The residual risk is judgement, not privilege: Gary could propose a colleague
the company does not need, which costs Alex the time to read and reject it,
and model tokens if approved.

## Joplin tools

- all note tools are confined to the Gary notebook (`JOPLIN_NOTEBOOK`) and its
  direct sub-notebooks; other notebooks are never matched;
- no tool returns note text, so existing notes cannot leak to OpenAI or be
  used for prompt injection through a conversation. The scheduled planning
  cycle reads only Gary › Planning notes titled like an active project and
  Gary's own previous daily summary; the model is told that text is data, and
  its only possible effect is calendar proposals that Python validates;
- deleting requires spoken confirmation and `confirmed: true`, accepts only
  note IDs listed or created in the current voice session, rechecks the note is
  still inside Gary, and moves one note to the Joplin trash rather than
  deleting it permanently;
- notes cannot be edited or moved, and notebooks cannot be deleted;
- the agent is told to put email content in a note only when asked.

## Git

The provided `.gitignore` ignores secrets and local data:

```text
.env
.env.bak*
client_secret.json
data/*   (except data/.gitkeep): token store, card vault, gary.db, backups
.models/
```

Always inspect `git status` before pushing a repository.

## UFW

Because the web interface is published only to `127.0.0.1`, you do not need an
incoming Internet-facing firewall rule for port 8000.

Do not add a broad inbound rule for this application.
