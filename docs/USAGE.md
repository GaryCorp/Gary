# Usage Guide

## Wake the assistant

Say the wake word naturally:

```text
Gary
```

You can immediately continue with the command:

```text
Gary, add lunch with Sam tomorrow at noon.
```

The pre-roll buffer is intended to preserve the beginning of the spoken request.

## Calendar examples

### Read events

```text
Gary, what's on my calendar today?
```

```text
Gary, read my events for tomorrow.
```

```text
Gary, am I free Friday afternoon?
```

```text
Gary, when is my next dentist appointment?
```

The assistant queries the primary calendar and reads matching events aloud in
chronological order. It reports when no events are found. Calendar reads are
limited to 25 events per request and a maximum 366-day range.

### Create events

```text
Gary, add a dentist appointment Friday at 2 PM.
```

```text
Gary, schedule project planning tomorrow at 10 AM for one hour.
```

```text
Gary, add dinner with Alex Saturday at 6:30 PM.
```

### Create all-day events

```text
Gary, add Mom's birthday on October 3rd.
```

```text
Gary, put my vacation on the calendar from July 7th through July 11th.
```

```text
Gary, mark Friday as a day off.
```

All-day events have no start or end time. For a multi-day event, say the first
and last day.

### Delete events

```text
Gary, delete my dentist appointment on Friday.
```

```text
Gary, cancel lunch with Sam tomorrow.
```

Gary looks the event up, reads it back, and asks you to confirm before deleting:

```text
User: Gary, delete my dentist appointment on Friday.
Assistant: I found Dentist on Friday at two PM. Should I delete it?
User: Yes.
Assistant: Done, I deleted Dentist on Friday.
```

If several events match, Gary asks which one. Only one event is deleted per
confirmation, and for a repeating event only that single occurrence is removed.
Calendar changes are only available by voice through Gary.

### Email

Gary can list **unread email in your Gmail Primary inbox** (promotions,
social, and no-reply senders are skipped), search all of your mail, reply, and
write new emails.

```text
Gary, do I have any new emails?
```

```text
Gary, what does Sam's email say?
```

```text
Gary, reply to Sam and say I'll be there at noon.
```

Gary always reads the reply back and asks before sending:

```text
User: Gary, reply to Sam and say I'll be there at noon.
Assistant: Here's the reply to Sam Lee: Hi Sam, I'll be there at noon. Should I send it?
User: Yes.
Assistant: Sent.
```

Say "change it to one o'clock" to revise, or "no" to cancel. Replies go only to
the original sender, in the same thread.

To find older or already-read email, including email you sent:

```text
Gary, did Alex email me about the lease last week?
```

```text
Gary, what did I send to Priya yesterday?
```

To write a new email:

```text
Gary, email Sam Lee and ask if Friday still works for lunch.
```

Gary looks up Sam's address from your past email, then reads back the
recipient, subject, and text and asks before sending. If you have never emailed
that address before, Gary also spells it out and asks you to confirm it:

```text
Assistant: You haven't emailed this address before. It's s, a, m, dot, l, e, e,
at example dot com. Is that right?
User: Yes.
```

New emails go to one recipient only, without CC or attachments, and at most
five per conversation. Gary cannot forward email, and ignores any instructions
written inside an email. Reading an email through Gary does not mark it as
read.

### New email announcements

Every hour Gary checks for new unread email in your Primary inbox and, if there
is any, says so:

```text
Assistant: You have 2 new emails: from Sam Lee about Lunch and from Alex about
the lease. Say Gary if you want to hear them.
```

Say "Gary, read the one from Sam" to hear it. Gary stays quiet from 10 PM to
7 AM and waits until you finish a conversation. Change the interval or quiet
hours in `.env` (see [Configuration](CONFIGURATION.md#new-email-check)).

### Notes (Joplin)

Gary can make notes in your **Gary** notebook in Joplin, and create new
notebooks inside it. The Joplin desktop app must be open with the Web Clipper
service enabled (see [Configuration](CONFIGURATION.md#joplin)).

```text
Gary, make a note: call the plumber about the kitchen sink.
```

```text
Gary, create a notebook called Groceries.
```

```text
Gary, add a note to Groceries: eggs, milk, and coffee.
```

```text
Gary, what notebooks do I have?
```

Notes go into Gary unless you name one of its notebooks. If that notebook
doesn't exist, Gary asks whether to create it. New notebooks are always created
inside Gary:

```text
Gary
├── (notes)
└── Groceries
    └── (notes)
```

To delete a note:

```text
User: Gary, delete my note about the plumber.
Assistant: I found Call the plumber in your Gary notebook. Should I delete it?
User: Yes.
Assistant: Done, I moved Call the plumber to the Joplin trash.
```

If several notes match, Gary asks which one. One note is deleted per
confirmation, and it goes to the Joplin trash, so you can restore it from there
in Joplin.

```text
Gary, what notes do I have in Groceries?
```

Gary can see note titles but not what notes say. He cannot edit or move notes,
delete notebooks, or touch notebooks outside Gary.

## Chief of Staff: projects, tasks, and planning

Gary keeps your projects, tasks, deadlines, dependencies, follow-ups, and
commitments in a local SQLite database (`data/gary.db`). It persists across
restarts, so Gary knows tomorrow what is done, blocked, overdue, and approved
without remembering the conversation.

### Set up a project

```text
Gary, I need to publish the Chief of Staff video by Friday.
```

Gary creates the project, breaks it into tasks, and records which tasks wait
on others (for example, editing waits on filming). You can also be explicit:

```text
Gary, add a task to film the demo, two hours, priority nine.
Gary, editing can't start until filming is done.
```

Priorities run from 1 (almost irrelevant) through 5 (normal) to 10 (critical).

### Plan and check status

```text
Gary, what should I work on?
Gary, what's blocked?
Gary, what's overdue?
```

Gary gathers everything in one step and recommends ready tasks in order of a
planning score the application calculates from priority, deadline urgency,
whether the task is overdue, whether other tasks wait on it, and whether it
fulfils a commitment. Your priority is never changed by the score.

### Progress, follow-ups, and commitments

```text
Gary, I finished filming.
Gary, remind me at 3 PM to check whether the upload finished.
Gary, I promised Sam the draft by Thursday.
```

Finishing a task closes its follow-ups and tells you what is now unblocked.
Gary announces due follow-ups and newly overdue tasks on his own, checking
every five minutes and staying quiet during `EMAIL_CHECK_QUIET_HOURS`.

### Scheduled planning

On weekdays at 8:00, 12:30, and 17:30, Gary plans on his own: he reviews your
projects and tasks, checks when your calendar is busy, reads your planning
notes, and may put up to five ready tasks on the calendar within working hours.
If you are near him, he says a short briefing:

```text
Assistant: I put filming on your calendar at one PM. Editing is still waiting on it.
```

Each run adds a section to a daily summary note in **Gary › Daily Summaries**
in Joplin, listing the plan, what was scheduled, what was not and why, and
overdue work, blockers, follow-ups, and commitments. To steer planning for a
project, write a note in **Gary › Planning** titled exactly like the project,
for example "Prefer mornings for filming." Scheduled runs never send email or
approve anything. Change the times or turn runs off in `.env` (see
[Configuration](CONFIGURATION.md#scheduled-planning)).

### Scheduling and approvals

```text
Gary, put filming on my calendar Thursday from 1 to 3.
```

Things Gary does outside the database become **actions**, and the application,
not Gary, decides their risk:

| Risk | What happens | Examples |
|---|---|---|
| Green | Runs immediately | schedule a task, move a normal task's calendar block, create or update tasks |
| Yellow | Waits for your approval | send an email Gary initiated, move the calendar block of a priority 8+ task or one tied to a commitment |
| Red | Refused | spend money, change security settings, access a password manager |

Approve or reject a yellow action by voice:

```text
Assistant: Emailing Sam with the subject "Draft" needs your approval. Should I send it?
User: Yes, approve it.
Assistant: Approved and sent.
```

or at `http://localhost:8000/approvals`, which shows the exact recipient, text,
or times. Unanswered approvals expire after 72 hours. Gary only reports an
action as done when it actually succeeded.

## Follow-up behavior

After an assistant response, a short follow-up period remains active.

Example:

```text
User: Gary, add a doctor's appointment tomorrow at 3.
Assistant: What should I call the event?
User: Annual physical.
```

The follow-up does not require another wake word while the active grace period
is still open.

## Return to sleep

The voice service automatically returns to local wake-word-only mode after:

- the active timeout; or
- the follow-up grace period expires.

The logs display:

```text
[assistant sleeping — say Gary to wake]
```

## View upcoming events

Open:

```text
http://localhost:8000/events
```

## Approvals page

```text
http://localhost:8000/approvals
```

Lists actions waiting for approval with Approve and Reject buttons, and
recently resolved ones with their outcome.

## Logs

All services:

```bash
docker compose logs -f
```

Voice only:

```bash
docker compose logs -f voice
```

Backend only:

```bash
docker compose logs -f backend
```
