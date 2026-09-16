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
security layer around the service.

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

These values are starting points and may need tuning on your laptop.

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

## Joplin tools

- all note tools are confined to the Gary notebook (`JOPLIN_NOTEBOOK`) and its
  direct sub-notebooks; other notebooks are never matched;
- no tool returns note text, so existing notes cannot leak to OpenAI or be
  used for prompt injection;
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
data/*   (except data/.gitkeep)
.models/
```

Always inspect `git status` before pushing a repository.

## UFW

Because the web interface is published only to `127.0.0.1`, you do not need an
incoming Internet-facing firewall rule for port 8000.

Do not add a broad inbound rule for this application.
