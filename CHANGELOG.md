# Changelog

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
