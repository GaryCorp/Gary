# Architecture

## Components

### 1. Voice container

Responsibilities:

- Use host audio through the PipeWire/PulseAudio socket.
- Capture 24 kHz mono microphone audio.
- Keep a short rolling pre-roll buffer.
- Resample a wake window to 16 kHz.
- Run `faster-whisper` locally.
- Detect the wake word `Gary`.
- Stream PCM audio to the backend only after activation.
- Speak the assistant's text replies with local Piper TTS.
- Mute the microphone stream while the assistant is speaking.

The voice container does **not** receive:

- the OpenAI API key;
- Google OAuth client credentials;
- Google refresh credentials;
- the Joplin token.

### 2. Backend container

Responsibilities:

- FastAPI web UI.
- Google OAuth.
- Encrypted Google credential persistence.
- WebSocket bridge from the voice service.
- OpenAI Realtime connection, opened per activation.
- Tool schema definition.
- Calendar, Gmail, and Joplin API execution.
- Chief of Staff operations database (SQLite), approvals page, and backups.
- Scheduled planning cycle (morning, midday, evening).
- The specialist team (Susan, Dave, Linda, Catherine, Lauren) through CrewAI, and its `/team` page.
- Lauren's EASE client, which calls the `ease-api` service.
- Catherine's encrypted debit card vault and the `/finance` page.
- Engineering tickets in the private GitHub repository and Project, and the
  read-only `/engineering/status` page.
- Hourly new email check, and follow-up and overdue alerts.
- Everything Gary says unprompted: recorded in SQLite, delivered to the voice
  service, and mirrored to the Gary › Spoken notebook.
- Input validation.

### 3. Google Calendar

The backend has four tools. They are available only to the voice agent as
OpenAI Realtime function tools; there are no web endpoints for creating or
deleting events.

```text
create_calendar_event
create_all_day_event
list_calendar_events
delete_calendar_event
```

`create_calendar_event` accepts:

- title
- start time
- end time
- optional description

`create_all_day_event` accepts:

- title
- first day (`YYYY-MM-DD`)
- optional last day, inclusive, for multi-day events (capped at 366 days)
- optional description

The backend converts the inclusive last day to Google's exclusive end date.

`list_calendar_events` accepts:

- start and end times;
- an optional result limit, capped at 25;
- an optional free-text query.

The read path expands recurring events, orders results chronologically, handles
timed and all-day entries, and returns only the event fields needed for a spoken
summary, including each `event_id`. Query ranges are capped at 366 days.

`delete_calendar_event` accepts:

- an `event_id`
- `confirmed`, which must be `true`

Deletion safeguards:

- The agent is instructed to list matching events, read the event back, and
  get an explicit yes before deleting.
- The backend rejects the call unless `confirmed` is `true`.
- The backend only deletes event IDs that were returned by
  `list_calendar_events` or created during the same voice session, so a
  guessed or hallucinated ID cannot delete anything.
- One event per call. For a recurring series, only that occurrence is deleted.

The backend validates all tool arguments before calling Google.

### 4. Gmail

The backend has six email tools, also available only to the voice agent:

```text
list_unread_emails
search_emails
find_email_contact
read_email
send_email_reply
send_new_email
```

- `list_unread_emails` searches `in:inbox is:unread category:primary`, skips
  no-reply senders, and returns sender, subject, received time, and a snippet
  (up to 10 emails).
- `search_emails` runs a Gmail search query over all mail except spam and
  trash (up to 10 emails), including sent mail.
- `find_email_contact` searches From/To/Cc headers for a name and returns up
  to 5 matching addresses, ranked by whether you have emailed them.
- `read_email` returns the plain-text body (HTML is converted to text), with
  quoted earlier replies removed, truncated to 3000 characters.
- `send_email_reply` sends a plain-text reply in the same thread.
- `send_new_email` sends a plain-text email in a new thread to one address.

Email safeguards:

- Only emails returned by `list_unread_emails` or `search_emails` in the same
  voice session can be read or replied to. Emails you sent cannot be replied to.
- The recipient is taken from the original email's `Reply-To` or `From`
  header. The tool has no recipient parameter, so text inside an email cannot
  redirect a reply. Replies to no-reply or bounce addresses are refused.
- `confirmed` must be `true`, after Gary reads the reply and recipient aloud.
- One reply per email per session; reply body capped at 5000 characters.
- New emails take exactly one plain address (no display names, lists, or CC).
  The backend checks your Sent mail; if you have never emailed the address,
  `new_recipient_confirmed` must also be `true`, after Gary spells the address
  out. At most 5 new emails per session, and no identical resends.
- Header values are reduced to a single line to prevent header injection.
- Tool results label email content as untrusted, and the agent is instructed
  never to follow instructions found inside email.

Scopes are `gmail.readonly` and `gmail.send`; the backend cannot modify or
delete mail.

#### Two mailboxes

With `GARY_EMAIL_ADDRESS` set, the encrypted token store holds two Google
accounts in separate slots: yours (`active_user_id`: calendar and your inbox)
and Gary's (`gary_user_id`: Gmail scopes only, signed in at `/login/gary`).
Both sign-ins use the same OAuth client and callback; the callback refuses any
account but `GARY_EMAIL_ADDRESS` in Gary's slot, and refuses that address in
yours. `list_unread_emails`, `search_emails`, and `find_email_contact` take a
`mailbox` (`user`, the default, or `gary`); each listed email remembers its
mailbox, so `read_email` and `send_email_reply` always go through the account
it came from. `send_new_email` and the planning `send_external_email` action
send from Gary's mailbox by default; `send_new_email` takes
`from_mailbox: "user"` when you ask. The new-recipient check counts your Sent
mail as well as Gary's. Gary's mailbox is used only while signed in as exactly
`GARY_EMAIL_ADDRESS`; otherwise its calls fail rather than fall back to yours.
The calendar always uses your account.

#### New email check

While the voice service is connected, the backend checks for unread Primary
inbox email received since the last check, every
`EMAIL_CHECK_INTERVAL_MINUTES`, in your inbox and, separately, in Gary's
mailbox if one is configured (announced as "Gary's inbox has ..."). It builds the announcement itself from sender
names and subjects and sends it to the voice service as a `bridge.announce`
message, which Piper speaks. OpenAI is not involved and Gary does not wake.

- Announced email IDs and the last check time are kept in backend memory, so a
  voice reconnect neither repeats nor misses announcements. A backend restart
  starts from the restart time.
- Checks during quiet hours are skipped without moving the last check time.
- The voice service holds announcements until an active conversation ends,
  and ignores the wake word while Gary is speaking.

### 5. Joplin notes

The backend has five note tools, also available only to the voice agent:

```text
list_joplin_notebooks
create_joplin_notebook
create_joplin_note
list_joplin_notes
delete_joplin_note
```

They call the Joplin desktop app's Web Clipper API through the `joplin-proxy`
service (see [Joplin proxy](#8-joplin-proxy-container)).

- `list_joplin_notebooks` returns the Gary notebook and its sub-notebooks.
- `create_joplin_notebook` creates a sub-notebook inside Gary, or reports that
  one with that name already exists.
- `create_joplin_note` creates a Markdown note in Gary or one of its
  sub-notebooks and returns the note's ID.
- `list_joplin_notes` returns note titles, notebook names, and update times,
  optionally filtered by words in the title.
- `delete_joplin_note` moves one note to the Joplin trash.

Note safeguards:

- Gary › Spoken is written by the backend, not by a tool: one note a day
  holding what Gary said out loud and when. Gary reads his own record from
  SQLite (`spoken_recent`), never from Joplin.
- No tool reads note text. `list_joplin_notes` returns titles, notebook names,
  and update times (up to 20, newest first). Notes cannot be edited or moved,
  and notebooks cannot be deleted. Only the scheduled planning cycle reads
  note text, limited to Gary › Planning notes titled like an active project
  and the previous daily summary.
- `delete_joplin_note` deletes one note per call and requires
  `confirmed: true`, after Gary reads the title and notebook aloud. Only
  note IDs listed or created in the same voice session are accepted, and the
  backend checks the note's current notebook is still inside Gary just before
  deleting. Notes go to the Joplin trash, not permanent deletion.
- Everything is confined to the `JOPLIN_NOTEBOOK` notebook (default `Gary`)
  and its direct sub-notebooks. Other notebooks are never matched by name.
- New notebooks are always created inside Gary, and an existing name is reused
  rather than duplicated.
- Titles are one line, up to 200 characters; bodies up to 20000 characters.

### 6. Chief of Staff operations (SQLite)

Structured operational state lives in `data/gary.db`, managed by the
`backend/gary` package. Joplin stays the place for human-readable notes.

```text
Gary (OpenAI Realtime)
   |  structured tool calls
   v
gary/tools        narrow tools, input validated with Pydantic models
   v
gary/services     business rules, policy, transactions, audit
   v
gary/db           repositories with parameterized SQL, migrations
   v
SQLite (WAL)      projects, tasks, task_dependencies, followups, commitments,
                  approvals, actions, audit_log, planning_runs
```

Tools exposed to Gary:

```text
project_create  project_create_with_tasks  project_list  project_get  project_update
task_create  task_update  task_complete  task_list  task_get
task_add_dependency  task_remove_dependency
followup_create  followup_complete  followup_list_due
commitment_create  commitment_update  commitment_list
planning_get_context  planning_get_brief  planning_find_work_blocks
planning_run_cycle  planning_record_plan
action_propose  approval_list_pending  approval_resolve
```

- `project_create_with_tasks` creates a project, its tasks, and dependencies
  in one transaction, so a goal costs one tool call instead of ten.
- `planning_get_brief` returns a deterministic brief built by
  `gary/services/briefing.py`: primary objective with deadline capacity
  (estimated remaining work against working time left), today's scheduled
  work, risks, decisions needed, and due follow-ups; midday adds finished and
  remaining work and earlier plans; evening adds completed, unfinished,
  blocked, moved, new follow-ups, and tomorrow.
- `planning_find_work_blocks` returns free blocks within `WORK_HOURS`, minus
  busy calendar time and `PROTECTED_TIMES` (`gary/services/calendar_blocks.py`).
- `planning_run_cycle` runs a planning cycle on request (not more than every
  10 minutes).
- `commitment_update` records an outcome directly, but changing what was
  promised becomes a yellow `change_external_commitment` action.

There is no tool to run SQL, open a shell, delete records or the database,
edit the audit log, or change policy.

Key rules:

- **Timestamps** must be ISO 8601 with an offset and are stored normalized to
  UTC, so SQL comparisons stay correct across daylight saving time. Tools show
  them in `LOCAL_TIMEZONE`.
- **Readiness**: a task is ready when it is `todo` or `scheduled`, every
  dependency is completed, its earliest start has passed, and its project is
  not `planned` (a planned project holds its work back). Circular
  dependencies are rejected inside a write-locked transaction.
- **Weekly video schedule** (`services/production.py`): a daily job, with no
  model call, that plans each batch of episodes as projects and tasks two
  weeks before the shoot, and puts the shoot and publish slots on the
  calendar through `schedule_task`. See USAGE.md.
- **Planning score** is deterministic Python: priority × 10, +40 overdue, +30 /
  +20 / +10 for deadlines within 24 / 72 / 168 hours, +15 if other open tasks
  depend on it, +20 if it fulfils an open commitment, and (project priority −
  5) × 2. It never overwrites priority.
- **IDs** passed to tools must be UUIDs.
- **Missed blocks**: a task whose `scheduled_end` has passed while it is not
  completed or cancelled appears in `missed_scheduled_blocks` with the open
  tasks downstream of it.
- **Working time**: scheduling or moving a task in conversation is refused
  outside `WORK_HOURS` and `PLANNING_WEEKDAYS` or over `PROTECTED_TIMES`,
  unless the payload sets `override_working_hours` because the user explicitly
  asked for that time.
- **`planning_get_context`** returns active projects, ready, in-progress,
  blocked, and overdue tasks, upcoming deadlines, due follow-ups, open
  commitments, pending approvals, and recent actions in one call, and starts a
  `planning_runs` row that `planning_record_plan` completes.
- **Actions**: `action_propose` validates the payload for the action type,
  `gary/policy.py` sets the risk (a handler may only escalate it), green runs,
  yellow creates an approval, red is rejected. Execution validates current
  state, runs the external call with no transaction open, then records success
  or failure in a short transaction and audits it. A failure is never recorded
  as success.
- **Approvals** are resolved by voice (`approval_resolve`, which needs
  `confirmed: true` and an approval shown in the same conversation) or on the
  `/approvals` page (session CSRF token and origin check). Pending approvals
  expire after 72 hours.
- **Audit log** is append-only, enforced by database triggers.
- **Alerts**: every `OPS_CHECK_INTERVAL_MINUTES` the backend announces due
  follow-ups, newly overdue tasks, and missed scheduled blocks once each
  (recorded in the audit log so a restart does not repeat them) and writes the
  daily backup. A missed block during working time triggers an
  `event_triggered` planning cycle, at most every two hours.

#### Scheduled planning cycle

`gary/services/planning_cycle.py` runs `morning`, `midday`, and `evening`
cycles in the backend at `PLANNING_TIMES` on `PLANNING_WEEKDAYS`, whether or
not the voice service is connected; `manual` cycles on request
(`planning_run_cycle`); and `event_triggered` cycles after a missed block:

```text
planning_get_context         SQLite state; starts a planning_runs row
BriefingService              the deterministic brief for this cycle type
JoplinPlanningNotebook       Gary > Planning notes titled like an active
                             project or "Preferences", plus the previous
                             daily summary
GoogleBusyCalendar           busy start/end times for the next 72 hours
GmailUnreadSummaries         sender, subject, snippet of up to 10 unread
                             Primary emails (PLANNING_EMAIL)
OpenAIPlanner                ONE Responses API call, strict JSON schema:
                             summary, spoken briefing, proposals
validate_cycle_actions       deterministic Python checks
action_propose               normal policy: green runs, yellow waits
complete_cycle               plan, results, and rejections saved + audited
write_daily_summary          the brief as a note section in
                             Gary > Daily Summaries > "Daily summary YYYY-MM-DD"
announce briefing            spoken if voice is connected and not quiet hours
```

Guardrails:

- One model call per run and no tool loop. The model can only propose
  `schedule_task`, `move_calendar_event`, and `create_followup`; it cannot
  send email, change tasks, or approve anything in a planning run.
- Every proposal is checked in Python before policy applies: the task exists
  and is open, `schedule_task` only for ready, unscheduled tasks, times have an
  offset, 15 to 240 minutes long, at least 10 minutes ahead and within 72
  hours, inside `WORK_HOURS` on a planning weekday, not over `PROTECTED_TIMES`,
  no overlap with busy times (other than the task's own event) or other
  proposals, moves of at least 30 minutes (smaller slips are not worth
  changing the calendar), one action per task, follow-ups due within 7 days
  and not duplicating a pending one, and at most `PLANNING_MAX_ACTIONS`.
  Rejections are recorded with reasons.
- If the calendar cannot be read, nothing is scheduled. If Joplin is closed,
  planning continues without notes and the summary is skipped.
- A scheduled run is started at most once per type per day, including failed
  runs, so a failure is never retried in a loop. A run missed while the backend
  was down is caught up within 90 minutes of its time. Requested cycles need 10
  minutes since the last cycle, event-triggered ones two hours.
- Runs are serialized, and all database work uses short transactions; no
  transaction is open during the model call or Google requests.

Action handlers:

| action_type | Risk | Effect |
|---|---|---|
| `create_internal_task`, `update_internal_task`, `create_followup` | green | SQLite only |
| `change_external_commitment` | yellow | changes a commitment's description, recipient, or deadline |
| `schedule_task` | green | creates a Google Calendar event, then stores its ID and times on the task; audited as `calendar_changed` |
| `move_calendar_event` | green, yellow for priority ≥ 8 or an open commitment | moves the task's event, then updates the task; audited as `calendar_changed` |
| `send_external_email` | yellow | sends one plain-text email; audited as `email_sent` |
| `card_purchase` | yellow, approvable only on the web page | Catherine's purchase request within the spending limits (rechecked at approval); recorded as approved, card not charged (no payment channel yet); audited as `card_purchase_approved` |
| `spend_money`, `change_security_settings`, `access_password_manager`, `change_own_permissions`, `modify_permissions`, `delete_audit_log` | red | refused |

### 7. GaryCorp specialist team

`backend/gary/agents/` adds five CrewAI specialists that Gary manages (see
[Team](TEAM.md)):

```text
Gary tool (delegate_to_agent, run_management_review, ...)
  -> AgentService      roster and limit checks, persisted assignment, audit
  -> GaryCorpAgentRunner
       context package  (per department, least privilege)
       ToolGateway      (granted tools: read-only, plus notes in the agent's own
                         Joplin notebook (read and write), Catherine's
                         purchase requests, and
                         Lauren's EASE analyses via ease-api;
                         every call checked and audited)
       CrewAIExecutor   (separate single-agent crew per assignment)
  -> validated Research / Security / Operations / Finance / Ethics report
  -> agent_assignments, agent_runs, audit_log; voice announcement
```

Gary's team tools: `team_list`, `delegate_to_agent`, `run_management_review`,
`management_review_follow_up`, `agent_assignment_get`,
`agent_assignments_list`, `management_review_get`.

`backend/gary/finance/` holds Catherine's card: `cards.py` validates a card,
encrypts it into `data/card_vault.enc`, and keeps brand, last four, expiry, and
status in `payment_cards`; `purchases.py` defines the `card_purchase` action
handler and the spending limits. Purchase requests reuse the actions,
approvals, and audit tables.

### 8. Joplin proxy container

Joplin desktop listens only on the host's `127.0.0.1:41184`, which containers
cannot reach. The `joplin-proxy` service runs `joplin_proxy/joplin_proxy.py` in
the stock `python:3.12-slim` image on the host network:

```text
backend container
    |
    v
172.30.99.1:41184   (assistant network gateway, joplin-proxy)
    |
    v
127.0.0.1:41184     (Joplin Web Clipper API)
```

It accepts connections only from `ASSISTANT_SUBNET`, holds no secrets (the
backend sends the Joplin token with each request), and closes the connection if
Joplin is not running, so the backend can tell the user to open Joplin.

### 9. Docker network

The voice and backend containers share the `assistant_net` bridge network,
which has a fixed subnet, `172.30.99.0/24`, and gateway `172.30.99.1`
(`ASSISTANT_SUBNET` and `ASSISTANT_GATEWAY`). `joplin-proxy` listens on that
gateway address from the host side. A fixed subnet lets a VPN such as NordVPN
allowlist it permanently.

## Audio flow

### Sleeping state

```text
microphone
    |
    v
voice container
    |
    +-- 24 kHz rolling buffer
    |
    +-- 24 kHz -> 16 kHz
            |
            v
      local faster-whisper
            |
            v
       wake-word test
```

No deliberate OpenAI microphone stream exists in this state.

### Active state (VOICE_MODE=transcribe, the default)

```text
pre-roll + microphone PCM24k
            |
            v
   Utterance (voice container)
   collects while the room is loud,
   ends after UTTERANCE_SILENCE_SECONDS
            |
            v   one WAV, plus its duration
        FastAPI
            |
            +--> transcription model  -> words   (billed per minute)
            |
            v
     text model (VOICE_TEXT_MODEL)
       |           |
       |           +--> calendar / email / notes / operations tool
       |                    |
       |      Google Calendar / Gmail / Joplin / SQLite
       |                    |
       |           <--------+  result appended, model called again
       v
  reply text (bridge.reply)
       |
       v
 voice container -> local Piper TTS -> speakers
```

Conversation history lives in the backend session and is cleared when Gary
sleeps, which is the lifetime the Realtime conversation had. The tool
definitions are identical in both modes: `Tool.schema()` emits the flat
`{"type": "function", "name", "parameters"}` shape that the Realtime and
Responses APIs both accept.

### Active state (VOICE_MODE=realtime)

```text
pre-roll + microphone PCM24k
            |
            v
        FastAPI
            |
            v
     OpenAI Realtime
       |           |
       |           +--> calendar / email / notes / operations tool
       |                    |
       |                    v
       |      Google Calendar / Gmail / Joplin / SQLite
       |
       v
 assistant text
       |
       v
 voice container
       |
       +-- local Piper TTS
       |
       v
   speakers
```

### Gary speaking first

Gary can also start the exchange, with no wake word and nobody having asked
him anything. Deciding to speak and actually speaking are deliberately
separate, because a voice service that is not listening must delay a message
rather than lose it:

```text
 approval requested / planning cycle / ask_user / new email / ops alert
            |
            v
   spoken_messages row  (status: pending)      <-- recorded before anything is said
            |
            v
      SpokenDelivery
       |          |
       |          +--> Gary > Spoken note in Joplin   (retried until written)
       v
 voice container --> local Piper TTS --> speakers
                                            |
                                            v
                              Alex says the wake word, in his own time
                                            |
                                            v
                               the normal active-state flow above
```

The microphone is never opened by the backend. A question Gary asks stays
open until Alex answers it or it expires, so the spoken text has to tell him
to say the wake word.

A message with `urgency: next_time` skips this path entirely. It is never
announced; it stays `pending` and is handed to the next voice session
(`send_outstanding_questions`), which marks it spoken and writes it to the
notebook because Gary is being told to raise it now. The same handover carries
questions still awaiting an answer, so a bare "yes" hours later is not a
mystery to the model.

Answers close the loop: `collect_operations` reports `open_questions` and
`answered_questions` to every planning cycle and to `planning_get_context`, so
what Alex said reaches Gary's planning, and `find_triggers` wakes the
management loop when a question is answered or is about to expire.

A message becomes `spoken` only once a voice client has taken it. Nothing
connected, or quiet hours, leaves it `pending` for the next delivery pass, and
a Joplin outage leaves `joplin_written_at` NULL and is retried. The caps on
how often Gary may do this live in `gary/services/conversation_service.py`;
see [SECURITY.md](SECURITY.md).

## Realtime tool calls

When the model makes several tool calls in one response (common when setting
up work), the backend sends each result as the call arrives and asks for one
follow-up response after `response.done`. A response that fails on OpenAI's
tokens-per-minute limit is retried after the suggested wait (at most 30
seconds, twice in a row), with a `bridge.notice` in the voice logs.

Each response carries the prompt and 48 tool schemas (roughly 11,500
tokens), so an OpenAI account with a 40,000 tokens-per-minute Realtime limit
can hit it during multi-step planning; the retry keeps the conversation going.

## Realtime session lifetime

OpenAI Realtime sessions expire after 60 minutes. The backend therefore opens a
Realtime connection when the first audio of an activation arrives, and closes it
when the voice service reports going back to sleep (`bridge.reset`).

Consequences:

- no upstream connection is held open while the assistant sleeps;
- sessions last as long as a conversation, so the 60-minute limit is not
  reached in normal use;
- if a session does expire mid-conversation, the next audio chunk transparently
  opens a new one and the voice service logs `[OpenAI session renewed]`;
- each new session gets fresh instructions, so the current date stays correct
  in a long-running deployment.

The wake-word loop is local and unaffected by upstream disconnects, so the
service can run indefinitely.

## Why separate containers

The separation reduces unnecessary secret exposure.

The microphone process can be restarted or modified without giving it the
OpenAI, Google, or Joplin credentials.

The backend can remain isolated from direct audio-device access.

Only the small `joplin-proxy` needs the host network, so the backend and voice
services stay on the isolated bridge network.

The specialists run inside the backend process, not in separate containers:
their isolation comes from the tool gateway and context packages (see
[Team](TEAM.md#permissions)), and they never receive the credentials the backend
holds.

## Host exposure

Docker publishes:

```text
127.0.0.1:8000
```

Only the local host can directly reach the published FastAPI port, which
serves:

```text
/               status and links
/login, /oauth2callback, /logout   Google sign-in
/events         upcoming calendar events
/approvals      pending approvals (GET) and decisions (POST, CSRF-protected)
/team           org chart, specialist assignment history, management reviews
/health         container health check
/internal/voice voice bridge WebSocket (bridge token required)
```

The voice-to-backend connection uses the Docker bridge network.

`joplin-proxy` listens only on the bridge gateway address, not on the LAN.

Outbound, the backend connects to OpenAI (Realtime during conversations, and
the Responses API for scheduled planning runs), Google APIs, and Joplin
through `joplin-proxy`.
