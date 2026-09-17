"""GitHub integration errors.

Every failure is one of these, so callers can tell a privacy refusal from a
permission problem from an outage. No error message ever carries the token:
the client redacts authorization headers before anything is logged or raised.
"""


class GitHubError(RuntimeError):
    """Base class for every GitHub integration failure."""

    status: str = "unavailable"


class GitHubConfigurationError(GitHubError):
    """A required setting is missing or malformed."""

    status = "misconfigured"


class GitHubAuthError(GitHubError):
    """401: the credential is missing, expired, or revoked."""

    status = "authentication_error"


class GitHubPermissionError(GitHubError):
    """403: the credential lacks a permission this operation needs."""

    status = "permission_error"


class GitHubNotFoundError(GitHubError):
    """404: the resource does not exist, or the credential cannot see it."""

    status = "misconfigured"


class GitHubValidationError(GitHubError):
    """422: GitHub rejected the request body."""

    status = "misconfigured"


class GitHubConflictError(GitHubError):
    """409: the resource changed under us."""

    status = "unavailable"


class GitHubRateLimitError(GitHubError):
    """429 or secondary rate limiting."""

    status = "unavailable"


class GitHubUnavailableError(GitHubError):
    """5xx, a network failure, or a timeout."""

    status = "unavailable"


class GitHubPrivacyError(GitHubError):
    """A repository or Project that must be private is public.

    Raised before any write. GaryCorp is proprietary: the integration refuses
    to operate rather than risk putting company work somewhere public, and it
    never changes visibility itself.
    """

    status = "privacy_error"
