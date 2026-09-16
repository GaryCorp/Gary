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
