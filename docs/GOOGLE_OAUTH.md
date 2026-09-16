# Google OAuth, Calendar, and Gmail Setup

## 1. Google Cloud project

Create or select a Google Cloud project.

## 2. Enable the Calendar and Gmail APIs

In **APIs & Services → Library**, enable both:

```text
Google Calendar API
Gmail API
```

## 3. Configure OAuth consent

Configure the OAuth consent screen for your account.

For a personal project in testing mode, make sure the Google account you intend
to use is allowed as a test user when Google requires it.

Under **Data access** (or **Scopes**), add the Gmail scopes listed in section 6.
`gmail.readonly` is a restricted scope: in testing mode it works for your test
users without Google verification, but Google shows an "unverified app"
warning during sign-in.

## 4. Create OAuth credentials

Create an OAuth client suitable for the local web flow.

Add this exact redirect URI:

```text
http://localhost:8000/oauth2callback
```

The URI must match exactly.

## 5. Download the client JSON

Save it to the project root as:

```text
client_secret.json
```

Do not commit this file. The provided `.gitignore` already ignores it.

## 6. Scopes requested by this project

The backend requests:

```text
openid
https://www.googleapis.com/auth/userinfo.email
https://www.googleapis.com/auth/calendar.events
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.send
```

`calendar.events` allows reading, creating, moving, and deleting calendar
events without requesting the broadest Calendar scope. Gary uses it for the
calendar voice tools, for scheduling and moving task blocks, and to read busy
times (start and end only) for scheduled planning.

`gmail.readonly` lets Gary list and read your email; it cannot change, label,
archive, or delete messages. `gmail.send` lets Gary send replies and new emails. The broader
`gmail.modify` and full-mailbox scopes are not requested. Reading an email
through Gary does not mark it as read.

## 7. Login

With the containers running, open:

```text
http://localhost:8000
```

Click the Google login link.

### Adding Gmail to an existing sign-in

If you signed in before Gmail support was added, the home page shows
**Gmail: not connected**. Click **Grant Gmail access** and approve the new
permissions. Make sure both Gmail boxes are ticked; the backend stores only the
scopes Google actually granted, and Gary tells you if Gmail access is missing.

## 8. Local token persistence

Google credential data is encrypted before being written to:

```text
data/token_store.enc
```

The encryption key is stored separately in `.env` as:

```text
TOKEN_ENCRYPTION_KEY
```

If you lose or change that key, the old encrypted token store cannot be
decrypted. Delete the token file and log in again.

## 9. Revoke local application state

To remove locally cached Google credentials:

```bash
docker compose down
rm -f data/token_store.enc
```

This removes the local encrypted copy. It does not itself revoke the OAuth grant
inside your Google account.
