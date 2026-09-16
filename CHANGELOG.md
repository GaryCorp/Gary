# Changelog

## 1.12.0 — Scheduled planning cycle

- Gary plans on his own on weekdays at 8:00, 12:30, and 17:30
  (`PLANNING_TIMES`, `PLANNING_WEEKDAYS`), whether or not voice is connected.
  Each run makes one OpenAI call (`PLANNING_MODEL`, default `gpt-5.4-mini`)
  with a strict JSON schema; there is no agent loop and no retry.
- The planner sees the operations state, busy calendar times (no titles),
  Gary › Planning notes titled like an active project, and the previous daily
  summary. It may only propose scheduling or moving task calendar blocks.
- Python validates every proposal (readiness, `WORK_HOURS`, 72-hour horizon,
  busy times, overlaps, at most `PLANNING_MAX_ACTIONS`) before normal policy.
- Each run is recorded in `planning_runs` with results and rejection reasons,
  appended to a daily summary note in Gary › Daily Summaries, and may speak a
  short briefing.
- `scripts/test.sh` runs the tests without `make`; the suite now has 90 tests.
- Verified live against OpenAI, Google Calendar (schedule and move), Gmail
  (approved send), and Joplin.

## 1.11.0 — Chief of Staff SQLite backend

- New `backend/gary` package: SQLite (`data/gary.db`, WAL) as the source of
  truth for projects, tasks, dependencies, follow-ups, commitments, approvals,
  actions, planning runs, and an append-only audit log, with versioned
  migrations, a repository/service/tool layering, and Pydantic validation.
- 20 new voice-agent tools, including `planning_get_context`, which returns
  ready, blocked, and overdue work ranked by a deterministic planning score.
- Actions go through policy as code: green runs, yellow waits for approval,
  red is refused. Supported actions: schedule or move a task's calendar event,
  send an email, create or update tasks.
- Approvals by voice or at `http://localhost:8000/approvals`; they expire after
  72 hours.
- The backend announces due follow-ups and newly overdue tasks, and makes daily
  SQLite backups (`OPS_CHECK_INTERVAL_MINUTES`, `GARY_BACKUP_KEEP`).
- `make test` runs the new test suite (76 tests) in the backend image.

## 1.10.0 — Delete Joplin notes

- Added `list_joplin_notes` (titles, notebooks, and update times only, never
  note text) and `delete_joplin_note` voice-agent tools.
- Deleting requires spoken confirmation and a `confirmed` flag, accepts only
  notes listed or created in the current voice session, and rechecks that the
  note is still inside the Gary notebook. Notes go to the Joplin trash.
- `create_joplin_note` now returns the note's ID, so a note just created can be
  deleted.

## 1.9.0 — Joplin notes

- Added `create_joplin_note`, `create_joplin_notebook`, and
  `list_joplin_notebooks` voice-agent tools. Notes go in the Joplin notebook
  `Gary` (`JOPLIN_NOTEBOOK`) or a notebook inside it; new notebooks are always
  created inside Gary. The tools cannot read, edit, or delete notes.
- New `joplin-proxy` service forwards the assistant network's gateway
  (`ASSISTANT_GATEWAY`, default `172.30.99.1`) to the Joplin desktop app's Web
  Clipper API on the host's `127.0.0.1:41184`.
- New setting `JOPLIN_TOKEN`: the Web Clipper authorization token.

## 1.8.1 — Works with NordVPN

- The Docker network between the voice and backend containers now uses a
  fixed subnet, `ASSISTANT_SUBNET` (default `172.30.99.0/24`), so it can be
  allowlisted in a VPN. Previously NordVPN's firewall blocked the voice
  container from reaching the backend (`timed out during opening handshake`).
- Run `nordvpn allowlist add subnet 172.30.99.0/24`; see
  `docs/TROUBLESHOOTING.md`.

## 1.8.0 — Hourly new email check

- The backend checks for new unread Primary inbox email every hour and Gary
  announces the count, senders, and subjects aloud with local Piper TTS. The
  check never contacts OpenAI.
- Configurable with `EMAIL_CHECK_INTERVAL_MINUTES` (default 60, 0 turns it
  off) and `EMAIL_CHECK_QUIET_HOURS` (default `22-7`). Email that arrives
  during quiet hours is announced at the first check afterwards.
- Announcements wait until an active conversation ends.
- The wake word is no longer detected while Gary is speaking, so an
  announcement that says "Gary" cannot wake him.

## 1.7.0 — Search and write email

- Added `search_emails` voice-agent tool: Gmail search across all mail,
  including read, archived, and sent email. Results can be read and replied to.
- Added `find_email_contact` to look up an address by name from past email.
- Added `send_new_email` to compose a new email to one recipient. Requires
  spoken confirmation; addresses you have never emailed must also be spelled
  back and confirmed (`new_recipient_confirmed`). Limited to 5 new emails and
  no duplicates per conversation.
- Replies to emails you sent yourself are refused.
- No new OAuth scopes.

## 1.6.0 — Runs indefinitely

- OpenAI Realtime sessions are now opened per wake-word activation and closed
  when the assistant sleeps, instead of one long-lived connection that hit the
  60-minute session limit (often mid-conversation).
- An expired session is replaced automatically on the next audio chunk, logged
  as `[OpenAI session renewed]`.
- Voice service now cancels its backend listener on reconnect, removing the
  "Task exception was never retrieved" traceback.

## 1.5.0 — Gmail replies

- Added `list_unread_emails`, `read_email`, and `send_email_reply` voice-agent
  tools for unread Primary inbox email.
- Replies require spoken confirmation, go only to the original sender in the
  same thread, and are limited to emails listed in the current session.
- New scopes `gmail.readonly` and `gmail.send`; the home page shows Gmail
  connection status with a Grant Gmail access link.
- Stored scopes now reflect what Google actually granted.

## 1.4.0 — Larger Whisper model

- Default wake-word model changed from `tiny` to `base.en` (downloaded during
  `docker compose up --build`); wake check interval raised to 1.25 s to match.
- `.models/` added to `.gitignore`.

## 1.3.0 — All-day events and deleting

- Added `create_all_day_event` tool for single and multi-day all-day events.
- Added `delete_calendar_event` tool with spoken confirmation, a required
  `confirmed` flag, and a backend check that only IDs listed or created in the
  current voice session can be deleted.
- `list_calendar_events` results can now be used to target deletions.
- New capabilities are agent tools only; no new web endpoints.

## 1.2.0 — Wake word Gary

- Default wake word changed from `AI` to `Gary` (configurable via `WAKE_WORD`).
- Gary's voice now uses local open-source Piper TTS; Realtime returns text only.
- Fixed choppy replies: no dropped playback audio; mic muted while Gary speaks.
- Optional NVIDIA GPU mode for local Whisper (`compose.gpu.yaml`).

## 1.1.0 — Calendar reading

- Added `list_calendar_events` as a Realtime function tool.
- Added spoken calendar summaries for date ranges and event searches.
- Added support for timed, all-day, and recurring event results.
- Added read-query validation: 25-result and 366-day limits.
- Updated usage, architecture, OAuth, and README documentation.

## Final complete package

- Dockerized FastAPI backend.
- Dockerized CPU/INT8 faster-whisper voice service.
- Wake word set to `AI`.
- Local pre-roll ring buffer.
- 24 kHz PCM stream for the Realtime connection.
- Local 24 kHz -> 16 kHz resampling for Whisper wake detection.
- OpenAI API key isolated to backend.
- Google OAuth isolated to backend.
- Encrypted Google token store.
- OAuth `state` validation.
- Internal bridge authentication token.
- Backend exposed only on host loopback.
- Container capability dropping.
- `no-new-privileges`.
- CPU, memory, and PID limits.
- Google Calendar event validation.
- Complete documentation set.
- Audio diagnostic scripts.
