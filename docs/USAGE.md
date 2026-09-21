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

Gary creates the project, its tasks with time estimates, and which tasks wait
on others (for example, editing waits on filming) in one step, checks your
calendar for free work blocks, schedules the tasks that can start, adds
checkpoints, and tells you whether the deadline is realistic:

```text
Assistant: Understood. I created the project with four tasks. Testing is the
critical path. I scheduled testing tomorrow from nine to ten thirty and filming
from one to four. Friday remains realistic.
```

Gary schedules inside working hours and never over protected time such as
lunch, unless you explicitly ask for a particular time. You can also be
explicit:

```text
Gary, add a task to film the demo, two hours, priority nine.
Gary, editing can't start until filming is done.
```

Priorities run from 1 (almost irrelevant) through 5 (normal) to 10 (critical).

### Plan and check status

```text
Gary, what should I work on today?
Gary, what's blocking the video?
Gary, what's due this week?
Gary, what am I behind on?
Gary, give me my morning brief.
Gary, replan the rest of today.
Gary, move the lower-priority work to tomorrow.
Gary, close out the day.
```

Gary gathers everything in one step and recommends ready tasks in order of a
planning score the application calculates from priority, the project's
priority, deadline urgency, whether the task is overdue, whether other tasks
wait on it, and whether it fulfils a commitment. Your priority is never changed
by the score.

The **morning brief** is short: the primary objective and whether its deadline
is realistic, today's scheduled work, risks, and any decision needed. Risks
include scheduled work whose time has passed while the task is unfinished, and
which tasks that holds up. "Replan the rest of today" and "close out the day"
run a full planning cycle (see below), which may move lower-priority work and
writes the day's summary note.

### Progress, follow-ups, and commitments

```text
Gary, I finished filming.
Gary, remind me at 3 PM to check whether the upload finished.
Gary, I promised Sam the draft by Thursday.
Gary, what commitments have I made?
Gary, remember that editing usually takes me two days.
```

Finishing a task closes its follow-ups and tells you what is now unblocked.
Marking a commitment fulfilled, missed, or cancelled happens directly, but
changing what was promised (such as a later deadline for Sam) needs your
approval. Preferences go in a **Preferences** note in **Gary › Planning**, which
planning reads.

When you ask Gary about an email that asks for something ("Can you send me the
draft by Thursday?"), he offers to record the commitment, create or update the
task, check your workload, schedule the work, and draft a reply. He only sends
the reply after the usual confirmation.

Gary announces due follow-ups, newly overdue tasks, and scheduled work that
passed unfinished on his own, checking every five minutes and staying quiet
during `EMAIL_CHECK_QUIET_HOURS`. When scheduled work passes unfinished during
working hours, he also replans (at most every two hours):

```text
Assistant: Filming is still incomplete and is blocking editing. I moved
optional research to tomorrow. Friday is still achievable. Moving the filming
block needs your approval.
```

### Scheduled planning

On weekdays at 8:00, 12:30, and 17:30, Gary plans on his own: the **morning
brief** sets up the day, the **midday review** compares reality with the
morning plan and replans only if something material changed, and the
**end-of-day review** records what was completed, unfinished, blocked, and
moved, and tomorrow's likely priority. Each run reviews your projects and
tasks, checks when your calendar is busy, reads your planning notes and the
sender, subject, and preview of unread email, and may schedule or move up to
five task blocks and create follow-ups, within working hours and outside
protected time. If you are near him, he says a short briefing:

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

Gary raises a yellow action with you out loud as soon as he proposes it, even
if you are not in a conversation, so nothing waits to be discovered on a web
page:

```text
Assistant: I need your approval for something. Email Sam with the subject "Draft".
           Say Gary when you want to answer, and tell me yes or no.
User: Gary, yes, approve it.
Assistant: Approved and sent.
```

The microphone only ever opens on the wake word, including here: Gary tells
you something needs you and then waits. He does not start listening on his
own, and the question stays open until you get to it.

You can still use `http://localhost:8000/approvals`, which shows the exact
recipient, text, or times, and it remains the only way to approve a card
purchase or a hire. Unanswered approvals expire after 72 hours. Gary only
reports an action as done when it actually succeeded.

## Letting the company run itself

GaryCorp keeps working between conversations: three planning cycles a day, a
management loop every fifteen minutes, six specialists, and the engineering
queue. Over days at a time three things keep it honest.

**A daily spending ceiling.** `MAX_DAILY_AI_SPEND_USD` (default $10) stops
everything that calls a model — including talking to Gary — until midnight
when it is reached. Gary still says so out loud, because speaking costs
nothing. Raise the value in `.env` and restart to lift it early.

> Check `python -m app.costs report` before leaving it alone for a week: the
> ceiling can only be enforced for models that have a price, and it tells you
> plainly when it cannot.

**Something noticing when the company is stuck.** Failed assignments, broken
engineering tickets, approvals that expired and questions you never answered
now count as problems and wake Gary, so a quiet week and a broken one no
longer look the same. What is stuck is in `/management/status` and in the
weekly review.

**A weekly review.** On Friday evening Gary writes `Weekly review YYYY-MM-DD`
in the Joplin Daily Summaries notebook — what was completed, what the team
was asked, what engineering moved, what was decided, what it cost, and what
needs you — and speaks a short summary. It is built from the database with no
model call, so you still get it on a week that hit the ceiling.

```text
Gary, what did the company do this week?
Gary, what's stuck?
Gary, what have we spent?
```

By default the company pauses Saturday and Sunday. See `MANAGEMENT_WEEKDAYS`
in [CONFIGURATION.md](CONFIGURATION.md) to run all seven days.

## Gary running your engineering queue

You are GaryCorp's Software/AI Engineer, and Gary is your manager. He opens
tickets in the private repository, assigns them to you, sets and changes their
priority, and puts the work on your calendar — in conversation, and on his own
between conversations.

```text
Gary, what's on my engineering queue?
Gary, make the retention bug a P0, the demo moved up.
Gary, what did you assign me this week?
```

Unprompted he is deliberately limited: at most one new ticket per planning
cycle, three a day, and two priority changes a cycle. He can only ticket work
that is already a task, so nothing appears that you have not at least seen as
a task first. Everything he opens is in the private Project, and he tells you
out loud when it matters.

## When Gary starts the conversation

Gary speaks first when something genuinely needs you: a decision only you can
make, a commitment about to be missed, an approval about to expire, a hire he
wants to propose. He will not interrupt you with status updates, and a
planning cycle may raise at most one thing.

Because speech does not persist, everything he says unprompted is recorded
first and written to a **Spoken** notebook in Joplin, one note a day:

```text
- **2:45 PM** Gary said: I need your decision on something. Hire Nina as Director of
  Customer Insight. _(waiting on your answer)_
```

So if you were out of the room, nothing is lost. Ask him directly:

```text
Gary, what did you say?
Gary, what did I miss?
Gary, say that again.
```

He can repeat something up to three times, after which he points you at the
notebook. If the voice service was down or it was quiet hours
(`EMAIL_CHECK_QUIET_HOURS`), the message stays queued and he says it at the
first opportunity rather than dropping it.

**Not everything interrupts you.** Gary decides whether something is worth
speaking now or worth holding. Anything he holds is never announced; it waits
and he works it into the next conversation you start:

```text
User: Gary, what's on this afternoon?
Assistant: Two blocks, filming at two and the edit at four. While you're here —
           the lease renewal is due in three weeks and needs an answer from you.
```

**And he drops it once you've answered.** What you say back is recorded and
goes into his planning, so he acts on it instead of asking again; a question
you have settled stays settled for three days. If you never get to a question,
it expires after 72 hours rather than following you around.

## The team: Susan, Dave, Linda, Catherine, and Lauren

Gary can ask specialists for help: Susan for research and strategy, Dave for
security, Linda for operations planning, Catherine for costs, budgets, and
purchases, and Lauren for ethical review with the EASE framework.

```text
Gary, have Susan research the best tools for recording screen demos.
Gary, get Dave's security assessment of the approvals page.
Gary, have Linda create an execution plan for the video project.
Gary, I'm thinking about giving you browser automation. Have your team evaluate it.
Gary, what did Susan find?
Gary, show me the management review.
Gary, what would a transcription subscription cost us per month?
Gary, have Catherine buy a USB microphone under forty dollars.
Gary, have Lauren check whether it's ethical to email past collaborators about the next video.
```

They work in the background, usually for a minute or two; Gary announces when
a report or review is ready. When several review the same question, they work
independently and Gary tells you where they disagree before recommending. See
[Team](TEAM.md) for details, limits, and the manual CLI.

Catherine never sees the card number and cannot charge the card. A purchase
she requests appears at `http://localhost:8000/approvals` with the merchant,
amount, and reason, within the spending limits. Only you can approve it, on
that page; Gary can reject one by voice but not approve it. No payment channel
is connected yet, so approving a purchase records it without charging the
card. Give her the card, freeze it, or remove it at
`http://localhost:8000/finance`.

Each specialist can also keep notes in their own Joplin notebook (**Susan**,
**Dave**, **Linda**, **Catherine**, **Lauren**). Ask for a write-up as part of the assignment:

```text
Gary, have Linda plan the prototype and write the plan up in her notebook.
```

Each specialist can also read their own notebook, so they can build on their
earlier notes, or on anything you write there yourself. They cannot read any
other notebook:

```text
Gary, have Susan check her notes on screen recording tools before researching microphones.
Gary, have Lauren check her notes on AI voiceovers and review the sponsor read.
```

## Engineering work

Gary can turn a company objective into an engineering ticket for you: a private
GitHub issue with the objective, requirements, and acceptance criteria,
assigned to you and placed on the private Engineering Project at Status Ready,
linked to one of his own tasks.

```text
Gary, we need Susan to have read-only web research. Make that an engineering ticket.
Gary, what engineering tickets are open?
Gary, mark the research integration ticket in progress.
Gary, that ticket is blocked on the API key.
Gary, is the engineering integration healthy?
```

Open the issue and work it with Claude Code; move the card on the Project
board, and Gary picks the change up (every 30 minutes by default, or when you
ask him to sync). When a ticket that required security review is done and the
issue closes, his task completes too.

Gary cannot change code, repository visibility, or GitHub permissions. If the
repository or Project is ever public, he refuses to create anything and says
so. See [Engineering](ENGINEERING.md) for setup and troubleshooting.

## Testing the autonomous loop

Before letting GaryCorp run unattended, watch it decide without letting it
act. `--act` is the switch that makes it real.

```bash
# What is the loop doing, and would it run right now? Free, no model call.
curl -s localhost:8000/management/status

# One real cycle: real model, real company data, nothing changes.
docker compose exec backend python -m app.dry_run

# Several in a row, or one of the scheduled cycle types.
docker compose exec backend python -m app.dry_run --cycles 3 --type morning

# Let it actually act: calendar, specialists, tokens.
docker compose exec backend python -m app.dry_run --act
```

Observe-only runs print what Gary *would* have done and why, write no daily
summary, and touch neither the calendar nor the team. An empty plan is a good
sign, not a broken one: most management ticks should find nothing to do.

To watch the live loop instead, lower `MANAGEMENT_TICK_MINUTES`, restart the
backend, and follow the log:

```bash
docker compose logs -f backend | grep -i management
```

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

Joplin proxy only:

```bash
docker compose logs -f joplin-proxy
```

Planning runs, actions, and approvals are recorded in `data/gary.db`
(`planning_runs`, `actions`, `approvals`, and the append-only `audit_log`); see
[Troubleshooting](TROUBLESHOOTING.md#scheduled-planning-did-not-run-or-did-nothing)
for a query.
