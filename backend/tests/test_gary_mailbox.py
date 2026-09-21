"""Gary's own mailbox, beside the user's Google account.

The user's account keeps the calendar and the user's inbox; Gary's account
(GARY_EMAIL_ADDRESS) is a second, email-only sign-in. The failures that
matter: the wrong Google account landing in either slot, an email read or
answered through the other mailbox, and Gary's mailbox being used when it is
not the configured address.
"""

import base64
import json
import os
import tempfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from google.oauth2.credentials import Credentials
from itsdangerous import TimestampSigner

from conftest import run

GARY = "garyhasaccess@gmail.com"
USER = "mainalextaylor@gmail.com"
TOKEN_URI = "https://oauth2.googleapis.com/token"


def import_main():
    """Import app.main against throwaway paths and secrets.

    The test container mounts the real /data, and app.main opens its
    database, token store and card vault at import, so every path is pointed
    at a temporary directory first and the environment is put back after.
    """
    scratch = Path(tempfile.mkdtemp(prefix="gary-mailbox-"))
    overrides = {
        "OPENAI_API_KEY": "sk-test-not-used",
        "SESSION_SECRET": "test-session-secret",
        "VOICE_BRIDGE_TOKEN": "test-bridge-token",
        "TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "CARD_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "GARY_DB_PATH": str(scratch / "gary.db"),
        "GARY_BACKUP_DIR": str(scratch / "backups"),
        "TOKEN_STORE_FILE": str(scratch / "token_store.enc"),
        "CARD_VAULT_FILE": str(scratch / "card_vault.enc"),
        "GARY_MODEL_PRICES_FILE": str(scratch / "model_prices.json"),
        "GARY_EMAIL_ADDRESS": GARY,
        "GITHUB_TOKEN": "",
        "EASE_API_URL": "",
        "OPENAI_ADMIN_KEY": "",
    }
    saved = {name: os.environ.get(name) for name in overrides}
    os.environ.update(overrides)
    try:
        import app.main as main
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    # If app.main had already been imported, it would hold the real paths.
    assert main.TOKEN_STORE_FILE == scratch / "token_store.enc"
    return main


main = import_main()


class FakeGmail:
    """One Gmail account: its messages, its sent folder, what it was asked."""

    def __init__(self, messages=(), sent_to=()):
        self.inbox = {message["id"]: message for message in messages}
        self.sent_to = set(sent_to)
        self.sent: list[dict] = []
        self.gets: list[str] = []

    # service.users().messages().<call>(...).execute()
    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q, maxResults):
        if q.startswith("in:sent"):
            hit = any(f"to:{address} " in q + " " for address in self.sent_to)
            return Call({"messages": [{"id": "sent-1"}]} if hit else {})
        return Call({"messages": [{"id": key} for key in self.inbox]})

    def get(self, userId, id, format, metadataHeaders=None):
        self.gets.append(id)
        return Call(self.inbox[id])

    def send(self, userId, body):
        self.sent.append(body)
        return Call({"id": f"sent-{len(self.sent)}"})


class Call:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


def email(message_id: str, sender: str, subject: str) -> dict:
    headers = {
        "From": sender,
        "To": "someone@example.com",
        "Subject": subject,
        "Date": "Mon, 21 Sep 2026 09:00:00 +0000",
        "Message-ID": f"<{message_id}@mail>",
    }
    return {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "labelIds": ["INBOX", "UNREAD"],
        "snippet": "hello",
        "internalDate": "0",
        "payload": {
            "mimeType": "text/plain",
            "headers": [{"name": k, "value": v} for k, v in headers.items()],
            "body": {"data": "aGVsbG8"},
        },
    }


def credentials(token: str) -> Credentials:
    return Credentials(
        token=token,
        refresh_token=None,
        token_uri=TOKEN_URI,
        client_id="client",
        client_secret="secret",
        scopes=main.SCOPES,
    )


@pytest.fixture
def accounts(monkeypatch, tmp_path):
    """A fresh token store and one fake Gmail per signed-in account."""
    store = main.EncryptedTokenStore(tmp_path / "token_store.enc")
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "GARY_EMAIL_ADDRESS", GARY)

    gmails = {
        "token-user": FakeGmail([email("u1", "Sam <sam@example.com>", "Lease")]),
        "token-gary": FakeGmail([email("g1", "Pat <pat@example.com>", "Hello Gary")]),
        "token-wrong": FakeGmail(),
    }
    monkeypatch.setattr(main, "gmail_service", lambda creds: gmails[creds.token])
    return store, gmails


def sign_in(store, mailbox: str, address: str, token: str, sub: str) -> None:
    run(store.save_user(sub, address, credentials(token), main.SCOPES, mailbox))


def new_session() -> dict:
    return {"emails": {}, "replied": set(), "new_emails": set()}


def test_signing_in_gary_leaves_the_users_account_and_calendar_alone(accounts):
    store, _ = accounts
    sign_in(store, "user", USER, "token-user", "sub-user")
    sign_in(store, "gary", GARY, "token-gary", "sub-gary")

    assert run(store.active_email()) == USER
    assert run(store.active_email("gary")) == GARY
    # The calendar and everything else keyed on the active user is unchanged.
    user_id, creds = run(main.credentials_for_active_user())
    assert (user_id, creds.token) == ("sub-user", "token-user")

    run(store.clear_active("gary"))
    assert run(store.active_email("gary")) is None
    assert run(store.active_email()) == USER


def test_gary_mailbox_fails_closed_until_the_configured_address_signs_in(accounts):
    store, _ = accounts
    sign_in(store, "user", USER, "token-user", "sub-user")

    with pytest.raises(RuntimeError, match="not connected"):
        run(main.credentials_for_mailbox("gary"))

    # A different account in Gary's slot is never used.
    sign_in(store, "gary", "someone-else@gmail.com", "token-wrong", "sub-wrong")
    with pytest.raises(RuntimeError, match="not connected"):
        run(main.credentials_for_mailbox("gary"))

    sign_in(store, "gary", GARY, "token-gary", "sub-gary")
    assert run(main.credentials_for_mailbox("gary")).token == "token-gary"


def test_gary_mailbox_is_refused_when_no_address_is_configured(accounts, monkeypatch):
    monkeypatch.setattr(main, "GARY_EMAIL_ADDRESS", "")
    assert main.default_send_mailbox() == "user"
    with pytest.raises(ValueError, match="GARY_EMAIL_ADDRESS is not set"):
        main.check_mailbox("gary")
    with pytest.raises(ValueError, match="mailbox must be"):
        main.check_mailbox("everyone")


def test_email_is_read_and_answered_through_the_mailbox_it_came_from(accounts):
    store, gmails = accounts
    sign_in(store, "user", USER, "token-user", "sub-user")
    sign_in(store, "gary", GARY, "token-gary", "sub-gary")
    session = new_session()

    listed = run(main.list_unread_emails(5, session, mailbox="gary"))
    assert listed["mailbox"] == "gary"
    assert [e["email_id"] for e in listed["emails"]] == ["g1"]

    read = run(main.read_email("g1", session))
    assert read["mailbox"] == "gary"
    assert "g1" in gmails["token-gary"].gets
    assert "g1" not in gmails["token-user"].gets

    reply = run(main.send_email_reply("g1", "Thanks", True, session))
    assert reply["from_mailbox"] == "gary"
    assert len(gmails["token-gary"].sent) == 1
    assert gmails["token-user"].sent == []


def test_the_users_inbox_is_still_the_default_for_reading(accounts):
    store, gmails = accounts
    sign_in(store, "user", USER, "token-user", "sub-user")
    sign_in(store, "gary", GARY, "token-gary", "sub-gary")
    session = new_session()

    listed = run(main.list_unread_emails(5, session))
    assert listed["mailbox"] == "user"
    assert [e["email_id"] for e in listed["emails"]] == ["u1"]

    run(main.send_email_reply("u1", "On it", True, session))
    assert len(gmails["token-user"].sent) == 1
    assert gmails["token-gary"].sent == []


def test_new_email_comes_from_gary_and_counts_the_users_contacts_as_known(accounts):
    store, gmails = accounts
    sign_in(store, "user", USER, "token-user", "sub-user")
    sign_in(store, "gary", GARY, "token-gary", "sub-gary")
    gmails["token-user"].sent_to.add("sam@example.com")

    sent = run(main.send_new_email(
        "sam@example.com", "Lease", "Following up", True, False, new_session()
    ))
    assert sent["from_mailbox"] == "gary"
    assert sent["first_email_to_recipient"] is False
    assert len(gmails["token-gary"].sent) == 1
    assert gmails["token-user"].sent == []


def test_a_stranger_still_needs_the_address_spelled_back(accounts):
    store, gmails = accounts
    sign_in(store, "user", USER, "token-user", "sub-user")
    sign_in(store, "gary", GARY, "token-gary", "sub-gary")

    with pytest.raises(ValueError, match="never emailed"):
        run(main.send_new_email(
            "new@example.com", "Hi", "Hello", True, False, new_session()
        ))
    assert gmails["token-gary"].sent == []


def test_the_user_can_still_send_from_their_own_address(accounts):
    store, gmails = accounts
    sign_in(store, "user", USER, "token-user", "sub-user")
    sign_in(store, "gary", GARY, "token-gary", "sub-gary")
    gmails["token-user"].sent_to.add("sam@example.com")

    sent = run(main.send_new_email(
        "sam@example.com", "Lease", "From me", True, False, new_session(),
        from_mailbox="user",
    ))
    assert sent["from_mailbox"] == "user"
    assert len(gmails["token-user"].sent) == 1
    assert gmails["token-gary"].sent == []


def test_sending_from_gary_fails_rather_than_falling_back_to_the_user(accounts):
    store, gmails = accounts
    sign_in(store, "user", USER, "token-user", "sub-user")

    with pytest.raises(RuntimeError, match="not connected"):
        run(main.send_new_email(
            "sam@example.com", "Lease", "Hello", True, True, new_session()
        ))
    assert gmails["token-user"].sent == []


def test_new_email_in_garys_inbox_is_announced_as_garys():
    one = main.new_email_announcement([("Pat", "Hello")], 1, "gary")
    assert one.startswith(f"{main.WAKE_WORD_DISPLAY}'s inbox has a new email from Pat")
    assert main.new_email_announcement([("Sam", "Lease")], 1).startswith("You have")


class FakeFlow:
    def __init__(self, email: str, verified: bool = True):
        self.code_verifier = "verifier"
        self.claims = {"sub": f"sub-{email}", "email": email, "email_verified": verified}
        self.credentials = credentials("token-new")
        self.credentials._id_token = "id-token"
        self.oauth2session = type("S", (), {"token": {"scope": main.GARY_MAILBOX_SCOPES}})()
        self.requested: dict = {}

    def authorization_url(self, **options):
        self.requested = options
        return "https://accounts.google.com/o/oauth2/auth", None

    def fetch_token(self, authorization_response):
        pass


@pytest.fixture
def web(accounts, monkeypatch):
    from starlette.testclient import TestClient

    flows: dict = {}

    def make_flow(state=None, mailbox="user"):
        flows["mailbox"] = mailbox
        return flows["flow"]

    monkeypatch.setattr(main, "make_flow", make_flow)
    monkeypatch.setattr(
        main.id_token, "verify_oauth2_token",
        lambda token, request, client_id: flows["flow"].claims,
    )
    client = TestClient(main.app, base_url="http://localhost:8000")
    return client, flows, accounts[0]


def oauth_round_trip(client, flows, login_path: str, email: str):
    flows["flow"] = FakeFlow(email)
    started = client.get(login_path, follow_redirects=False)
    assert started.status_code in (302, 307)

    # Google sends the state back unchanged; read it from the session cookie.
    raw = TimestampSigner("test-session-secret").unsign(client.cookies.get("session"))
    oauth_state = json.loads(base64.b64decode(raw))["oauth_state"]
    return client.get(
        f"/oauth2callback?state={oauth_state}&code=abc", follow_redirects=False
    )


def test_gary_sign_in_asks_google_for_garys_account_and_no_calendar(web):
    client, flows, store = web
    response = oauth_round_trip(client, flows, "/login/gary", GARY)

    assert flows["mailbox"] == "gary"
    assert flows["flow"].requested["login_hint"] == GARY
    assert response.status_code in (302, 307)
    assert run(store.active_email("gary")) == GARY
    assert run(store.active_email()) is None
    assert "https://www.googleapis.com/auth/calendar.events" not in main.GARY_MAILBOX_SCOPES


def test_gary_sign_in_refuses_any_other_account(web):
    client, flows, store = web
    response = oauth_round_trip(client, flows, "/login/gary", USER)

    assert response.status_code == 400
    assert "Nothing was changed" in response.text
    assert run(store.active_email("gary")) is None


def test_garys_account_cannot_take_the_users_calendar_slot(web):
    client, flows, store = web
    sign_in(store, "user", USER, "token-user", "sub-user")

    response = oauth_round_trip(client, flows, "/login", GARY)

    assert response.status_code == 400
    assert run(store.active_email()) == USER
    assert run(store.active_email("gary")) is None
