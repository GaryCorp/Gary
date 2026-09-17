"""Configuration and credentials for the engineering-ticket integration.

The token is read from the environment and never stored in SQLite, Joplin,
issue bodies, prompts, or the audit log. Credentials sit behind
``GitHubCredentialProvider`` so a GitHub App can replace the personal access
token later without touching the API client.
"""

import os
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from gary.integrations.github.exceptions import GitHubConfigurationError

REST_API_URL = "https://api.github.com"
GRAPHQL_API_URL = "https://api.github.com/graphql"

_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


@runtime_checkable
class GitHubCredentialProvider(Protocol):
    """Supplies a bearer token for one API call.

    v1 reads a token from the environment. A GaryCorp GitHub App would
    implement the same call and mint short-lived installation tokens.
    """

    async def get_token(self) -> str: ...


class EnvTokenProvider:
    """Reads the token from the environment once, at construction."""

    def __init__(self, token: str):
        token = (token or "").strip()
        if not token:
            raise GitHubConfigurationError(
                "GITHUB_TOKEN is not set, so the engineering integration has no credential"
            )
        self._token = token

    async def get_token(self) -> str:
        return self._token


@dataclass(frozen=True)
class GitHubConfig:
    """Where GaryCorp's private engineering work lives. No name is hard-coded."""

    owner: str
    repository: str
    project_number: int
    engineer_username: str
    # Filled in by setup once discovered; both are non-secret GitHub node ids.
    project_id: str | None = None
    rest_url: str = REST_API_URL
    graphql_url: str = GRAPHQL_API_URL
    timeout: float = 30.0
    max_attempts: int = 3

    def __post_init__(self):
        for field, value, pattern in (
            ("GITHUB_OWNER", self.owner, _LOGIN),
            ("GITHUB_ENGINEER_USERNAME", self.engineer_username, _LOGIN),
            ("GITHUB_REPOSITORY", self.repository, _REPOSITORY),
        ):
            if not pattern.match(value or ""):
                raise GitHubConfigurationError(f"{field} is missing or not a valid GitHub name")
        if not isinstance(self.project_number, int) or self.project_number < 1:
            raise GitHubConfigurationError("GITHUB_PROJECT_NUMBER must be a positive integer")

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repository}"

    @classmethod
    def from_env(cls, environ: dict | None = None) -> "GitHubConfig":
        env = environ if environ is not None else os.environ
        number = (env.get("GITHUB_PROJECT_NUMBER") or "").strip()
        if not number.isdigit():
            raise GitHubConfigurationError("GITHUB_PROJECT_NUMBER must be a positive integer")
        return cls(
            owner=(env.get("GITHUB_OWNER") or "").strip(),
            repository=(env.get("GITHUB_REPOSITORY") or "").strip(),
            project_number=int(number),
            engineer_username=(env.get("GITHUB_ENGINEER_USERNAME") or "").strip(),
            project_id=(env.get("GITHUB_PROJECT_ID") or "").strip() or None,
        )


def configured(environ: dict | None = None) -> bool:
    """True when every required setting is present, without validating them."""
    env = environ if environ is not None else os.environ
    return all(
        (env.get(name) or "").strip()
        for name in (
            "GITHUB_TOKEN",
            "GITHUB_OWNER",
            "GITHUB_REPOSITORY",
            "GITHUB_PROJECT_NUMBER",
            "GITHUB_ENGINEER_USERNAME",
        )
    )
