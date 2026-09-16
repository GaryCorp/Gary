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
goes or which note to delete. Gary never reads the text of existing notes, and
cannot see notebooks outside Gary.

## OpenAI key

The API key is not placed in the voice container.

## Whisper model

The selected `faster-whisper` model is downloaded while building the voice
image. Runtime wake-word transcription then uses the local model files.

## Docker is not a privacy boundary by itself

Docker helps isolate dependencies and reduce secret exposure, but the voice
container has explicit microphone access. Only run code and images you trust.
