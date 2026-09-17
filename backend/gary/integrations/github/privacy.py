"""The private-only gate.

GaryCorp is proprietary. Every engineering write checks first that the
configured repository and Project are private and owned by the configured
owner, and refuses otherwise. Checks fail closed: an error reading visibility
is treated as unsafe, never as "probably fine". Nothing here can change
visibility; only Alex can, by hand, in GitHub.
"""

import logging
from dataclasses import dataclass

from gary.integrations.github.client import GitHubClient
from gary.integrations.github.exceptions import GitHubError, GitHubPrivacyError
from gary.integrations.github.models import Project, Repository

logger = logging.getLogger("gary.github.privacy")

# Visibility is re-checked at least this often, even within one process.
PRIVACY_CACHE_SECONDS = 300


@dataclass(frozen=True)
class PrivacyReport:
    repository_private: bool | None
    project_private: bool | None
    owner_matches: bool
    project_owner_matches: bool
    safe_to_operate: bool
    detail: str

    def as_dict(self) -> dict:
        return {
            "repository_private": self.repository_private,
            "project_private": self.project_private,
            "owner_matches": self.owner_matches,
            "project_owner_matches": self.project_owner_matches,
            "safe_to_operate": self.safe_to_operate,
            "detail": self.detail,
        }


class PrivacyGate:
    def __init__(self, client: GitHubClient, cache_seconds: float = PRIVACY_CACHE_SECONDS):
        self._client = client
        self._cache_seconds = cache_seconds
        self._checked_at: float | None = None
        self._cached: tuple[Repository, Project] | None = None

    def invalidate(self) -> None:
        self._checked_at = None
        self._cached = None

    async def verify(self, monotonic=None) -> tuple[Repository, Project]:
        """Return the repository and Project, or raise GitHubPrivacyError.

        Raises before any write, so a public resource stops the operation
        instead of receiving company data.
        """
        import time

        clock = monotonic or time.monotonic
        now = clock()
        if self._cached and self._checked_at is not None and now - self._checked_at < self._cache_seconds:
            return self._cached

        config = self._client.config
        repository = await self._client.get_repository()
        if not repository.private:
            raise GitHubPrivacyError(
                f"Refusing to operate because configured GaryCorp repository "
                f"{repository.full_name} is public. Make it private in GitHub; "
                "Gary will not change visibility."
            )
        if repository.owner.casefold() != config.owner.casefold():
            raise GitHubPrivacyError(
                f"Refusing to operate: GitHub returned repository owner {repository.owner!r}, "
                f"but GITHUB_OWNER is {config.owner!r}."
            )

        project = await self._client.get_project()
        if project.public:
            raise GitHubPrivacyError(
                f"Refusing to operate because the configured Project "
                f"{project.title!r} (number {project.number}) is public. Make it private "
                "in GitHub; Gary will not change visibility."
            )
        if project.owner_login.casefold() != config.owner.casefold():
            raise GitHubPrivacyError(
                f"Refusing to operate: Project {project.number} belongs to "
                f"{project.owner_login!r}, not to GITHUB_OWNER {config.owner!r}."
            )

        self._cached = (repository, project)
        self._checked_at = now
        return self._cached

    async def audit(self) -> PrivacyReport:
        """A report Gary can read. Never raises: an unreachable GitHub is an
        unsafe result, not an exception."""
        config = self._client.config
        repository_private: bool | None = None
        project_private: bool | None = None
        owner_matches = False
        project_owner_matches = False
        try:
            repository = await self._client.get_repository()
            repository_private = repository.private
            owner_matches = repository.owner.casefold() == config.owner.casefold()
            project = await self._client.get_project()
            project_private = project.private
            project_owner_matches = project.owner_login.casefold() == config.owner.casefold()
        except GitHubError as exc:
            logger.warning("GitHub privacy audit could not complete: %s", exc)
            return PrivacyReport(
                repository_private=repository_private,
                project_private=project_private,
                owner_matches=owner_matches,
                project_owner_matches=project_owner_matches,
                safe_to_operate=False,
                detail=str(exc),
            )

        safe = bool(repository_private and project_private and owner_matches and project_owner_matches)
        if safe:
            detail = (
                f"{config.full_name} and Project {config.project_number} are private "
                f"and owned by {config.owner}."
            )
        else:
            problems = []
            if not repository_private:
                problems.append("the repository is public")
            if not project_private:
                problems.append("the Project is public")
            if not owner_matches:
                problems.append("the repository owner does not match GITHUB_OWNER")
            if not project_owner_matches:
                problems.append("the Project owner does not match GITHUB_OWNER")
            detail = "Not safe to operate: " + ", ".join(problems) + "."
        return PrivacyReport(
            repository_private=repository_private,
            project_private=project_private,
            owner_matches=owner_matches,
            project_owner_matches=project_owner_matches,
            safe_to_operate=safe,
            detail=detail,
        )
