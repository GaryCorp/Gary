"""Google sign-in: the encrypted token store, the OAuth flow, and which
account (the user's, or Gary's own mailbox) a call goes through.
"""

import asyncio
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from app.config import (
    CLIENT_SECRETS_FILE,
    GARY_EMAIL_ADDRESS,
    GARY_MAILBOX,
    GARY_MAILBOX_SCOPES,
    MAILBOXES,
    REDIRECT_URI,
    SCOPES,
    TOKEN_ENCRYPTION_KEY,
    TOKEN_STORE_FILE,
    USER_MAILBOX,
    WAKE_WORD_DISPLAY,
)


fernet = Fernet(TOKEN_ENCRYPTION_KEY.encode())


class EncryptedTokenStore:
    """Google credentials, encrypted at rest.

    Holds up to two signed-in accounts, one per mailbox: the user's
    (active_user_id: calendar and the user's inbox) and Gary's own
    (gary_user_id: Gary's inbox only). Signing one in never touches the other.
    """

    SLOT_KEYS = {USER_MAILBOX: "active_user_id", GARY_MAILBOX: "gary_user_id"}

    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()

    def _read_unlocked(self) -> dict:
        if not self.path.exists():
            return {"active_user_id": None, "gary_user_id": None, "users": {}}

        try:
            decrypted = fernet.decrypt(self.path.read_bytes())
            data = json.loads(decrypted.decode())
        except (InvalidToken, json.JSONDecodeError) as exc:
            raise RuntimeError("Encrypted token store is unreadable") from exc

        data.setdefault("active_user_id", None)
        data.setdefault("gary_user_id", None)
        data.setdefault("users", {})
        return data

    def _write_unlocked(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(fernet.encrypt(json.dumps(data).encode()))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    async def save_user(
        self,
        user_id: str,
        email: str,
        credentials: Credentials,
        granted_scopes: list[str],
        mailbox: str = USER_MAILBOX,
    ) -> None:
        async with self._lock:
            data = self._read_unlocked()
            data["users"][user_id] = {
                "email": email,
                "token": credentials.token,
                "refresh_token": credentials.refresh_token,
                "token_uri": credentials.token_uri,
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret,
                "scopes": granted_scopes,
            }
            data[self.SLOT_KEYS[mailbox]] = user_id
            self._write_unlocked(data)

    async def active_user_id(self, mailbox: str = USER_MAILBOX) -> str | None:
        async with self._lock:
            return self._read_unlocked().get(self.SLOT_KEYS[mailbox])

    async def active_email(self, mailbox: str = USER_MAILBOX) -> str | None:
        async with self._lock:
            data = self._read_unlocked()
            user_id = data.get(self.SLOT_KEYS[mailbox])
            if not user_id:
                return None
            return data["users"].get(user_id, {}).get("email")

    async def active_scopes(self, mailbox: str = USER_MAILBOX) -> list[str]:
        async with self._lock:
            data = self._read_unlocked()
            user_id = data.get(self.SLOT_KEYS[mailbox])
            if not user_id:
                return []
            return data["users"].get(user_id, {}).get("scopes", [])

    async def get_credentials(self, user_id: str) -> Credentials | None:
        async with self._lock:
            data = self._read_unlocked()
            stored = data["users"].get(user_id)
            if not stored:
                return None

            credentials = Credentials(
                token=stored["token"],
                refresh_token=stored.get("refresh_token"),
                token_uri=stored["token_uri"],
                client_id=stored["client_id"],
                client_secret=stored["client_secret"],
                scopes=stored["scopes"],
            )

            if credentials.expired and credentials.refresh_token:
                credentials.refresh(GoogleRequest())
                stored["token"] = credentials.token
                self._write_unlocked(data)

            return credentials

    async def clear_active(self, mailbox: str = USER_MAILBOX) -> None:
        async with self._lock:
            data = self._read_unlocked()
            data[self.SLOT_KEYS[mailbox]] = None
            self._write_unlocked(data)


store = EncryptedTokenStore(TOKEN_STORE_FILE)


def make_flow(state: str | None = None, mailbox: str = USER_MAILBOX) -> Flow:
    return Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=GARY_MAILBOX_SCOPES if mailbox == GARY_MAILBOX else SCOPES,
        redirect_uri=REDIRECT_URI,
        state=state,
    )


async def credentials_for_active_user() -> tuple[str, Credentials]:
    user_id = await store.active_user_id()
    if not user_id:
        raise RuntimeError(
            "No Google account is active. Sign in at http://localhost:8000"
        )

    credentials = await store.get_credentials(user_id)
    if not credentials:
        raise RuntimeError("Google credentials are unavailable")

    return user_id, credentials


def gary_mailbox_configured() -> bool:
    return bool(GARY_EMAIL_ADDRESS)


def check_mailbox(mailbox: str) -> str:
    mailbox = (mailbox or USER_MAILBOX).strip().lower()
    if mailbox not in MAILBOXES:
        raise ValueError("mailbox must be 'user' or 'gary'")
    if mailbox == GARY_MAILBOX and not gary_mailbox_configured():
        raise ValueError(
            "There is no separate mailbox for " + WAKE_WORD_DISPLAY + "; GARY_EMAIL_ADDRESS is not set"
        )
    return mailbox


async def credentials_for_mailbox(mailbox: str) -> Credentials:
    """Credentials for the user's mailbox or Gary's.

    Gary's mailbox fails closed: it must be signed in, and signed in as
    exactly GARY_EMAIL_ADDRESS, or nothing is read or sent through it.
    """
    mailbox = check_mailbox(mailbox)
    if mailbox == USER_MAILBOX:
        _, credentials = await credentials_for_active_user()
        return credentials

    user_id = await store.active_user_id(GARY_MAILBOX)
    email = (await store.active_email(GARY_MAILBOX) or "").lower()
    if not user_id or email != GARY_EMAIL_ADDRESS:
        raise RuntimeError(
            f"{WAKE_WORD_DISPLAY}'s mailbox is not connected. Tell the user to open "
            f"http://localhost:8000 and sign in {GARY_EMAIL_ADDRESS} under "
            f"{WAKE_WORD_DISPLAY}'s mailbox."
        )
    credentials = await store.get_credentials(user_id)
    if not credentials:
        raise RuntimeError(f"{WAKE_WORD_DISPLAY}'s Google credentials are unavailable")
    return credentials


def default_send_mailbox() -> str:
    """New emails come from Gary's own address whenever one is configured."""
    return GARY_MAILBOX if gary_mailbox_configured() else USER_MAILBOX
