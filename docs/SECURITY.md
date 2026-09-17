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
  Gary's planning notes), except `write_note`, which only creates notes in the
  agent's own top-level Joplin notebook (fixed in the roster, never chosen by
  the model), at most 3 per assignment, each stamped and audited, and
  Catherine's `request_card_purchase`, which only creates a purchase request
  you approve on the web page (see [Catherine's debit card](#catherines-debit-card)),
  and Lauren's `run_ease_analysis`, which sends a question to the local EASE
  service (once per assignment) and changes nothing;
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
requests within the limits that wait for Alex on the approvals page.

EASE (`ease-api`) listens on host loopback (`127.0.0.1:8001`) and the assistant
Docker network, with no API key by default, so any process on this machine can
use it and spend its model key; set `EASE_API_KEY` to require a key. It holds
its own copy of the model key and runs with all capabilities dropped.

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
