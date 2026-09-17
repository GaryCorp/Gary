# Privacy Behavior

## Sleeping state

While waiting for the wake word, microphone blocks remain in the local voice
container for:

- the rolling wake window;
- the rolling pre-roll window;
- local Whisper inference.

The design does not intentionally transmit microphone audio to OpenAI during the
sleeping state.

## Wake activation

When local Whisper recognizes `Gary`, the voice service forwards:

- the configured pre-roll interval;
- live microphone audio during the active session.

That means a small amount of audio immediately before the wake word is
transmitted by design.

## Why pre-roll exists

Without pre-roll, wake detection itself consumes time. The beginning of:

```text
Gary, add a meeting...
```

could otherwise be cut off.

## Google data

Calendar and Gmail requests go through the backend and the Google APIs.

## Email content

When you ask about email, the sender, recipients, subject, and snippet of
listed or searched messages, the names and addresses found by a contact lookup,
and the text of any email Gary reads, are sent to OpenAI as part of the
conversation so Gary can summarize them and draft emails. Search queries
Gary builds from your request can match any mail except spam and trash. Apart
from the new email check below, nothing is fetched unless you ask. Replies and
new emails are sent only after you confirm them aloud.

The hourly new email check reads the sender and subject of new unread Primary
inbox email in the backend only; none of it is sent to OpenAI. Gary speaks the
senders and subjects aloud, so anyone nearby can hear them. Set
`EMAIL_CHECK_INTERVAL_MINUTES=0` to turn it off.

Google OAuth credentials are stored locally in encrypted form.

## Joplin notes

What you ask Gary to note is sent to OpenAI as part of the conversation, like
any request, and saved to your local Joplin app. The names of notebooks inside
Gary, and the titles of notes in them, are sent when Gary looks up where a note
goes or which note to delete. Gary never reads the text of existing notes,
and cannot see notebooks outside Gary, with one exception: planning (the
planning cycle below, and `planning_get_context` in conversation) reads
Planning notes titled like an active project or "Preferences", and Gary's
previous daily summary.

## Projects and tasks

The operations database stays on your machine in `data/gary.db`, with daily
backups in `data/backups`. When Gary uses the operations tools, the returned
project, task, follow-up, commitment, approval, and action details are sent to
OpenAI as part of the conversation. The periodic follow-up and overdue check
runs in the backend only; Gary speaks the titles aloud.

## Scheduled planning cycle

Each planning cycle (scheduled by default at 8:00, 12:30, and 17:30 on
weekdays, run on request with "replan" or "close out the day", or after
scheduled work passes unfinished) makes one request to OpenAI
(`PLANNING_MODEL`) containing:

- the same operations state `planning_get_context` returns: projects, tasks,
  deadlines, follow-ups, commitments, pending approvals, and recent actions;
- your busy calendar times for the next 72 hours, as start and end times only,
  with no event titles, attendees, or descriptions;
- the deterministic brief: today's scheduled work, completed and unfinished
  tasks, risks, pending approvals, and earlier plans from today;
- the text (up to 3000 characters each) of notes in **Gary › Planning** whose
  title matches an active project name or is "Preferences", and of Gary's
  previous daily summary note. No other note is read;
- the sender name, subject, and Gmail's short preview (about 200 characters) of
  up to 10 unread Primary inbox emails, never bodies or addresses. Set
  `PLANNING_EMAIL=subjects` to leave out previews, or `off` to leave out email.

The request is sent with `store: false`. The daily summary is written to
**Gary › Daily Summaries** in Joplin, and a short briefing may be spoken aloud.
Set `PLANNING_TIMES` empty to turn scheduled runs off.

## Specialist team

When Gary delegates to Susan, Dave, Linda, Catherine, or Lauren, their context package and tool
results are sent to OpenAI (`GARY_EMPLOYEE_MODEL`): the assignment and any
context Gary includes, the related project and its tasks, Gary's planning notes
for that project, and depending on the specialist, commitments, follow-ups,
free calendar blocks (without event titles), the permission and policy
configuration, recent audit summaries, or Catherine's card brand, last four
digits, expiry, spending totals, purchase requests, and team token usage. No
email content, credentials, or card number are included. Susan's and
Catherine's web searches send the search query to OpenAI's web search
(`AGENT_WEB_SEARCH_MODEL`). Lauren's EASE analyses send the decision question
and context she writes to the local `ease-api` container, which sends them to
its own model provider (`EASE_LLM_PROVIDER`, OpenAI by default); EASE stores
nothing (it has no database) and its logs stay in Docker. Reports are stored locally in `data/gary.db`, and
notes the specialists write are saved in their own notebooks in your local
Joplin app. When a specialist reads their own notebook (**Susan**, **Dave**,
**Linda**, **Catherine**, or **Lauren**), the titles they list and the text of
the notes they open (up to 10,000 characters each) are sent to OpenAI as part
of their work, so do not keep anything in those notebooks you would not send to
OpenAI. No other notebook is read.
CrewAI telemetry and hosted tracing are disabled.

## Engineering tickets

Ticket titles, objectives, requirements, acceptance criteria, dependencies,
estimates, deadlines, and the Gary task id are sent to GitHub as issue content,
in the **private** repository and **private** Project named by `GITHUB_OWNER`,
`GITHUB_REPOSITORY`, and `GITHUB_PROJECT_NUMBER`. Nothing is sent if either is
public. Credentials, card details, email content, and anything shaped like a
token or password are scrubbed before an issue is written. GitHub identifiers
(issue number, node id, Project item id) come back into `data/gary.db`; the
GitHub token stays in the environment.

## Debit card

The card number, expiry, and name you enter at `/finance` stay on this machine,
encrypted in `data/card_vault.enc`. Nothing sends them anywhere: no payment
channel is connected, and no model ever receives them. The merchant, amount,
and description of purchase requests go to OpenAI as part of Catherine's work.

## OpenAI key

The API key is not placed in the voice container.

## Whisper model

The selected `faster-whisper` model is downloaded while building the voice
image. Runtime wake-word transcription then uses the local model files.

## Docker is not a privacy boundary by itself

Docker helps isolate dependencies and reduce secret exposure, but the voice
container has explicit microphone access. Only run code and images you trust.
