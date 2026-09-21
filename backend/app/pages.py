"""The operator's web pages: Google sign-in for both mailboxes, upcoming
events, approvals, the team, and Catherine's card.

Loopback only and unauthenticated by design (see SECURITY.md); every form
that changes something is CSRF-protected and refuses other origins. The
company objects come from ``request.app.state``, set by app.main.
"""

import asyncio
import datetime as dt
import html
import json
import secrets
import urllib.parse
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token

from app.config import (
    GARY_EMAIL_ADDRESS,
    GARY_EMPLOYEE_MODEL,
    GARY_MAILBOX,
    GMAIL_READ_SCOPE,
    GMAIL_SEND_SCOPE,
    LOCAL_TIMEZONE,
    MAILBOXES,
    SPENDING_LIMITS,
    USER_MAILBOX,
    WAKE_WORD_DISPLAY,
)
from app.google_auth import gary_mailbox_configured, make_flow, store
from app.google_calendar import calendar_service
from app.local_time import spoken_time
from gary.db.repositories import Repositories
from gary.finance import cards as finance_cards
from gary.finance.purchases import purchase_brief, spending_status
from gary.policy import CFO_ACTOR, USER_ACTOR

router = APIRouter()


@router.get("/")
async def home(request: Request):
    gary_ops = request.app.state.gary_ops
    email = request.session.get("email") or await store.active_email()

    if email:
        google_status = (
            f"<p>Google account: <strong>{html.escape(email)}</strong></p>"
        )

        scopes = await store.active_scopes()
        if GMAIL_READ_SCOPE in scopes and GMAIL_SEND_SCOPE in scopes:
            google_status += "<p>Gmail: connected</p>"
        else:
            google_status += (
                '<p>Gmail: not connected. <a href="/login">Grant Gmail access</a></p>'
            )
    else:
        google_status = '<p><a href="/login">Sign in with Google</a></p>'

    if gary_mailbox_configured():
        gary_email = await store.active_email(GARY_MAILBOX)
        gary_scopes = await store.active_scopes(GARY_MAILBOX)
        if (
            gary_email
            and gary_email.lower() == GARY_EMAIL_ADDRESS
            and GMAIL_READ_SCOPE in gary_scopes
            and GMAIL_SEND_SCOPE in gary_scopes
        ):
            google_status += (
                f"<p>{html.escape(WAKE_WORD_DISPLAY)}'s mailbox: "
                f"<strong>{html.escape(gary_email)}</strong> connected "
                '(<a href="/logout/gary">disconnect</a>)</p>'
            )
        else:
            google_status += (
                f"<p>{html.escape(WAKE_WORD_DISPLAY)}'s mailbox: not connected. "
                f'<a href="/login/gary">Sign in {html.escape(GARY_EMAIL_ADDRESS)}</a></p>'
            )

    pending = await asyncio.to_thread(gary_ops.approvals.list_pending)
    approvals_link = (
        f"<strong>Approvals ({len(pending)} waiting)</strong>"
        if pending
        else "Approvals"
    )

    return HTMLResponse(
        f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>AI Calendar Assistant</title>
</head>
<body>
  <h1>AI Calendar Assistant</h1>
  {google_status}
  <p>
    Local Whisper listens for the wake word <strong>{html.escape(WAKE_WORD_DISPLAY)}</strong>.
    Microphone audio is not intentionally sent to OpenAI until activation.
  </p>
  <p>
    <a href="/events">Upcoming events</a>
    ·
    <a href="/approvals">{approvals_link}</a>
    ·
    <a href="/team">Team</a>
    ·
    <a href="/finance">Finance</a>
    ·
    <a href="/logout">Logout</a>
  </p>
</body>
</html>"""
    )


@router.get("/login")
async def login(request: Request):
    return start_google_login(request, USER_MAILBOX)


@router.get("/login/gary")
async def login_gary(request: Request):
    if not gary_mailbox_configured():
        return HTMLResponse(
            "GARY_EMAIL_ADDRESS is not set, so there is no mailbox to connect.",
            status_code=400,
        )
    return start_google_login(request, GARY_MAILBOX)


def start_google_login(request: Request, mailbox: str) -> RedirectResponse:
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    request.session["oauth_mailbox"] = mailbox

    flow = make_flow(state, mailbox)
    options = {}
    if mailbox == GARY_MAILBOX:
        # Preselect Gary's account so the browser's usual one is not reused.
        options["login_hint"] = GARY_EMAIL_ADDRESS
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent select_account",
        **options,
    )
    # The callback builds a new Flow, so keep the PKCE verifier for it.
    request.session["oauth_code_verifier"] = flow.code_verifier
    return RedirectResponse(authorization_url)


@router.get("/oauth2callback")
async def oauth2callback(request: Request):
    expected_state = request.session.pop("oauth_state", None)
    received_state = request.query_params.get("state")

    if (
        not expected_state
        or not received_state
        or not secrets.compare_digest(expected_state, received_state)
    ):
        return HTMLResponse(
            "OAuth state validation failed.",
            status_code=400,
        )

    mailbox = request.session.pop("oauth_mailbox", USER_MAILBOX)
    if mailbox not in MAILBOXES:
        return HTMLResponse("Unknown mailbox.", status_code=400)

    flow = make_flow(received_state, mailbox)
    flow.code_verifier = request.session.pop("oauth_code_verifier", None)
    flow.fetch_token(authorization_response=str(request.url))
    credentials = flow.credentials

    claims = id_token.verify_oauth2_token(
        credentials.id_token,
        GoogleRequest(),
        credentials.client_id,
    )

    user_id = claims["sub"]
    email = claims.get("email", user_id)

    # Keep the two accounts apart: Gary's slot takes only GARY_EMAIL_ADDRESS,
    # and the user's slot (the calendar) never takes Gary's account.
    signed_in_as = (email or "").lower()
    if mailbox == GARY_MAILBOX and (
        signed_in_as != GARY_EMAIL_ADDRESS or not claims.get("email_verified")
    ):
        return HTMLResponse(
            f"{html.escape(WAKE_WORD_DISPLAY)}'s mailbox must be "
            f"{html.escape(GARY_EMAIL_ADDRESS)}, but Google signed in "
            f"{html.escape(email)}. Nothing was changed. "
            '<a href="/login/gary">Try again</a> and choose the right account.',
            status_code=400,
        )
    if (
        mailbox == USER_MAILBOX
        and GARY_EMAIL_ADDRESS
        and signed_in_as == GARY_EMAIL_ADDRESS
    ):
        return HTMLResponse(
            f"{html.escape(email)} is {html.escape(WAKE_WORD_DISPLAY)}'s mailbox, "
            "not your account, so it cannot hold your calendar. Nothing was "
            f'changed. Use <a href="/login/gary">{html.escape(WAKE_WORD_DISPLAY)}\'s '
            "mailbox sign-in</a> for it.",
            status_code=400,
        )

    # Store the scopes Google actually granted; the user can untick some.
    granted = flow.oauth2session.token.get("scope") or credentials.scopes or []
    if isinstance(granted, str):
        granted = granted.split()

    await store.save_user(user_id, email, credentials, list(granted), mailbox)

    if mailbox == USER_MAILBOX:
        request.session["user_id"] = user_id
        request.session["email"] = email

    return RedirectResponse("/")


@router.get("/events")
async def events(request: Request):
    user_id = request.session.get("user_id") or await store.active_user_id()
    if not user_id:
        return RedirectResponse("/login")

    credentials = await store.get_credentials(user_id)
    if not credentials:
        return RedirectResponse("/login")

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    service = calendar_service(credentials)

    result = await asyncio.to_thread(
        lambda: service.events()
        .list(
            calendarId="primary",
            timeMin=now,
            maxResults=10,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )

    items = []
    for event in result.get("items", []):
        start = event["start"].get(
            "dateTime",
            event["start"].get("date", ""),
        )
        items.append(
            "<li>"
            f"<strong>{html.escape(event.get('summary', 'Untitled'))}</strong>"
            f" — {html.escape(start)}"
            "</li>"
        )

    return HTMLResponse(
        "<h1>Upcoming events</h1>"
        "<ul>"
        + "".join(items)
        + "</ul>"
        '<p><a href="/">Back</a></p>'
    )


ALLOWED_ORIGINS = {"http://localhost:8000", "http://127.0.0.1:8000"}


def approval_csrf_token(request: Request) -> str:
    token = request.session.get("approval_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["approval_csrf"] = token
    return token


def payload_html(payload: dict) -> str:
    rows = []
    for key, value in payload.items():
        if isinstance(value, str) and key in {"start", "end", "new_start", "new_end", "deadline", "due_at"}:
            value = spoken_time(value)
        text = value if isinstance(value, str) else json.dumps(value)
        rows.append(
            f"<dt>{html.escape(key)}</dt>"
            f'<dd><pre style="white-space:pre-wrap;margin:0">{html.escape(text)}</pre></dd>'
        )
    return "<dl>" + "".join(rows) + "</dl>"


@router.get("/approvals")
async def approvals_page(request: Request):
    gary_ops = request.app.state.gary_ops
    token = approval_csrf_token(request)
    message = request.session.pop("approval_message", None)
    pending = await asyncio.to_thread(gary_ops.approvals.list_pending)
    resolved = await asyncio.to_thread(gary_ops.approvals.list_recent_resolved, 10)

    cards = []
    for approval in pending:
        payload = json.loads(approval["payload_json"])
        cards.append(
            '<section style="border:1px solid #999;padding:0.5em 1em;margin:1em 0">'
            f"<h2>{html.escape(approval['summary'])}</h2>"
            f"<p>Action: <code>{html.escape(approval['action_type'])}</code> · "
            f"Risk: <strong>{html.escape(approval['risk_level'])}</strong> · "
            f"Requested {html.escape(spoken_time(approval['created_at']))}</p>"
            f"<p>Reason: {html.escape(approval['reason'] or '(none given)')}</p>"
            f"{payload_html(payload)}"
            f'<form method="post" action="/approvals/{html.escape(approval["id"])}">'
            f'<input type="hidden" name="csrf" value="{html.escape(token)}">'
            '<button name="decision" value="approved">Approve</button> '
            '<button name="decision" value="rejected">Reject</button>'
            "</form></section>"
        )

    history = "".join(
        "<li>"
        f"{html.escape(approval['summary'])}: <strong>{html.escape(approval['status'])}</strong>"
        + (
            f" (action {html.escape(approval['action_status'])})"
            if approval["action_status"]
            else ""
        )
        + (
            f" — {html.escape(approval['action_error'])}"
            if approval["action_error"]
            else ""
        )
        + "</li>"
        for approval in resolved
    )

    return HTMLResponse(
        f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Approvals</title>
</head>
<body>
  <h1>Approvals</h1>
  {f"<p><strong>{html.escape(message)}</strong></p>" if message else ""}
  {"".join(cards) or "<p>Nothing is waiting for approval.</p>"}
  <h2>Recently resolved</h2>
  <ul>{history or "<li>None yet.</li>"}</ul>
  <p><a href="/">Back</a></p>
</body>
</html>"""
    )


@router.post("/approvals/{approval_id}")
async def resolve_approval(request: Request, approval_id: str):
    gary_ops = request.app.state.gary_ops
    origin = request.headers.get("origin")
    if origin is not None and origin not in ALLOWED_ORIGINS:
        return HTMLResponse("Cross-origin request refused", status_code=403)

    form = urllib.parse.parse_qs((await request.body()).decode("utf-8", "replace"))
    csrf = (form.get("csrf") or [""])[0]
    expected = request.session.get("approval_csrf")
    if not expected or not secrets.compare_digest(csrf, expected):
        return HTMLResponse("Invalid or expired form; reload the page", status_code=403)

    decision = (form.get("decision") or [""])[0]
    try:
        result = await gary_ops.approvals.resolve(
            approval_id, decision, actor=USER_ACTOR, channel="web"
        )
    except ValueError as exc:
        request.session["approval_message"] = str(exc)
    else:
        execution = result.get("execution")
        if execution is None:
            outcome = "Rejected."
        elif execution["status"] == "succeeded":
            outcome = (execution.get("result") or {}).get("note") or "Approved and done."
        else:
            outcome = f"Approved, but it failed: {execution.get('error')}"
        request.session["approval_message"] = f"{result['summary']}: {outcome}"

    return RedirectResponse("/approvals", status_code=303)


@router.get("/team")
async def team_page(request: Request):
    agent_service = request.app.state.agent_service
    team = await asyncio.to_thread(agent_service.team)
    history = await asyncio.to_thread(agent_service.list_assignments, None, None, 15)

    def esc(value) -> str:
        return html.escape(str(value)) if value is not None else ""

    def members(parent: str | None, depth: int = 0) -> str:
        rows = ""
        for member in team["members"]:
            if member["reports_to"] != parent:
                continue
            counts = ", ".join(f"{k} {v}" for k, v in sorted(member["assignments"].items())) or "no assignments"
            rows += (
                f'<li style="margin-left:{depth * 1.5}em"><strong>{esc(member["name"])}</strong> — '
                f'{esc(member["title"])} · Status: {esc(member["status"].capitalize())} · {esc(counts)}</li>'
            )
            rows += members(member["agent_id"], depth + 1)
        return rows

    def outcome(item: dict) -> str:
        report = item["report"] or {}
        if "risk_level" in report:
            return f"Risk: {report['risk_level']}, {report['recommendation']}"
        if "deadline_assessment" in report:
            return f"Deadline assessment: {report['deadline_assessment']}"
        if "budget_assessment" in report:
            requested = len(report.get("purchase_request_ids", []))
            return f"Budget: {report['budget_assessment']}, purchase requests: {requested}"
        if "ethical_assessment" in report:
            ease = "EASE used" if report.get("ease_analyses") else "EASE not used"
            return f"Ethics: {report['ethical_assessment'].replace('_', ' ')}, {ease}"
        if "confidence" in report:
            return f"Confidence: {report['confidence']:.0%}"
        return item["error"] or ""

    rows = "".join(
        "<tr>"
        f"<td>{esc(item['agent'].split(',')[0])}</td>"
        f"<td>{esc(item['status'])}</td>"
        f"<td>{esc(item['objective'][:140])}</td>"
        f"<td>{esc(outcome(item))}</td>"
        f"<td>{esc((item['report'] or {}).get('summary', '')[:200])}</td>"
        f"<td>{esc(item['created_at'][:16].replace('T', ' '))}</td>"
        "</tr>"
        for item in history
    )
    reviews = "".join(
        f"<li>{esc(r['topic'][:140])} — {esc(r['status'])} ({esc(r['created_at'][:16].replace('T', ' '))})</li>"
        for r in team["recent_reviews"]
    )
    return HTMLResponse(
        f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>GaryCorp Team</title>
</head>
<body>
  <h1>GaryCorp Team</h1>
  <ul>{members(None)}</ul>
  <h2>Recent assignments</h2>
  <table border="1" cellpadding="4" style="border-collapse:collapse">
    <tr><th>Agent</th><th>Status</th><th>Objective</th><th>Outcome</th><th>Summary</th><th>Assigned</th></tr>
    {rows or '<tr><td colspan="6">No assignments yet.</td></tr>'}
  </table>
  <h2>Management reviews</h2>
  <ul>{reviews or "<li>None yet.</li>"}</ul>
  <p>Model: {esc(GARY_EMPLOYEE_MODEL)} · Limits: {esc(team["limits"])}</p>
  <p><a href="/">Back</a></p>
</body>
</html>"""
    )


def finance_origin_refused(request: Request) -> bool:
    origin = request.headers.get("origin")
    return origin is not None and origin not in ALLOWED_ORIGINS


@router.get("/finance")
async def finance_page(request: Request):
    gary_ops = request.app.state.gary_ops
    card_vault = request.app.state.card_vault
    token = approval_csrf_token(request)
    message = request.session.pop("finance_message", None)
    timezone = ZoneInfo(LOCAL_TIMEZONE)

    def read():
        with gary_ops.db.read() as conn:
            repos = Repositories.bind(conn)
            card = finance_cards.public_card(repos.finance.current_card(CFO_ACTOR))
            spending = spending_status(repos, SPENDING_LIMITS, timezone, dt.datetime.now(dt.timezone.utc))
            purchases = [purchase_brief(row, timezone) for row in repos.finance.list_purchases(limit=20)]
        return card, spending, purchases

    card, spending, purchases = await asyncio.to_thread(read)
    esc = html.escape
    csrf = f'<input type="hidden" name="csrf" value="{esc(token)}">'

    if card:
        toggle = "unfreeze" if card["status"] == "frozen" else "freeze"
        card_html = (
            f"<p><strong>{esc(card['brand'])} ending {esc(card['last4'])}</strong> · "
            f"expires {esc(card['expires'])} · status: <strong>{esc(card['status'])}</strong></p>"
            f'<form method="post" action="/finance/card/{toggle}" style="display:inline">{csrf}'
            f"<button>{toggle.capitalize()} card</button></form> "
            f'<form method="post" action="/finance/card/remove" style="display:inline">{csrf}'
            "<button>Remove card</button></form>"
            "<h3>Replace the card</h3>"
        )
    else:
        card_html = "<p>Catherine has no card yet.</p><h3>Give Catherine a card</h3>"

    if card_vault.configured:
        form = (
            f'<form method="post" action="/finance/card" autocomplete="off">{csrf}'
            '<p><label>Card number <input name="number" inputmode="numeric" autocomplete="off" required></label></p>'
            '<p><label>Expiry month <input name="exp_month" size="2" inputmode="numeric" required></label> '
            '<label>year <input name="exp_year" size="4" inputmode="numeric" required></label></p>'
            '<p><label>Name on card <input name="name_on_card" autocomplete="off"></label></p>'
            "<p>The security code is not stored. The number is encrypted on this machine; "
            "Catherine and Gary only see the brand and last four digits.</p>"
            "<button>Save card</button></form>"
        )
    else:
        form = "<p>Set <code>CARD_ENCRYPTION_KEY</code> in <code>.env</code> (run setup.sh) to add a card.</p>"

    rows = "".join(
        "<tr>"
        f"<td>{esc(p['requested_at'][:16].replace('T', ' '))}</td><td>{esc(p['merchant'] or '')}</td>"
        f"<td>{esc(p['description'] or '')}</td><td>{esc(p['amount'])}</td>"
        f"<td>{esc(p['status'])}{(' — ' + esc(p['error'])) if p['error'] else ''}</td>"
        "</tr>"
        for p in purchases
    )
    return HTMLResponse(
        f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Finance</title>
</head>
<body>
  <h1>Finance</h1>
  {f"<p><strong>{esc(message)}</strong></p>" if message else ""}
  <h2>Catherine's debit card</h2>
  {card_html}
  {form}
  <h2>Spending this month</h2>
  <p>Committed {esc(spending['committed_this_month'])} of {esc(spending['monthly_limit'])}
  ({esc(spending['remaining_this_month'])} left) · per-purchase limit {esc(spending['per_purchase_limit'])}.
  Committed means requested and waiting for approval, or approved.</p>
  <p>No payment channel is connected, so approved purchases are not charged to the card.
  Approve or reject requests on the <a href="/approvals">approvals page</a>.</p>
  <h2>Purchase requests</h2>
  <table border="1" cellpadding="4" style="border-collapse:collapse">
    <tr><th>Requested</th><th>Merchant</th><th>For</th><th>Amount</th><th>Status</th></tr>
    {rows or '<tr><td colspan="5">None yet.</td></tr>'}
  </table>
  <p><a href="/">Back</a></p>
</body>
</html>"""
    )


@router.post("/finance/card/{operation}")
@router.post("/finance/card")
async def change_finance_card(request: Request, operation: str = "add"):
    gary_ops = request.app.state.gary_ops
    card_vault = request.app.state.card_vault
    if finance_origin_refused(request):
        return HTMLResponse("Cross-origin request refused", status_code=403)
    form = urllib.parse.parse_qs((await request.body()).decode("utf-8", "replace"))
    field = lambda name: (form.get(name) or [""])[0]  # noqa: E731
    expected = request.session.get("approval_csrf")
    if not expected or not secrets.compare_digest(field("csrf"), expected):
        return HTMLResponse("Invalid or expired form; reload the page", status_code=403)

    try:
        if operation == "add":
            today = dt.datetime.now(ZoneInfo(LOCAL_TIMEZONE)).date()
            card = await asyncio.to_thread(
                finance_cards.add_card, gary_ops.db, card_vault, field("number"),
                field("exp_month"), field("exp_year"), field("name_on_card"), today,
            )
            message = f"Saved. Catherine now holds the {card['brand']} card ending {card['last4']}."
        elif operation in ("freeze", "unfreeze"):
            card = await asyncio.to_thread(finance_cards.set_frozen, gary_ops.db, operation == "freeze")
            message = f"The card ending {card['last4']} is now {card['status']}."
        elif operation == "remove":
            await asyncio.to_thread(finance_cards.remove_card, gary_ops.db, card_vault)
            message = "The card was removed and its encrypted details deleted."
        else:
            return HTMLResponse("Unknown operation", status_code=404)
    except (ValueError, finance_cards.CardVaultError) as exc:
        message = str(exc)
    request.session["finance_message"] = message
    return RedirectResponse("/finance", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    await store.clear_active()
    return RedirectResponse("/")


@router.get("/logout/gary")
async def logout_gary(request: Request):
    # Disconnects Gary's mailbox only; the user's account stays signed in.
    await store.clear_active(GARY_MAILBOX)
    return RedirectResponse("/")
