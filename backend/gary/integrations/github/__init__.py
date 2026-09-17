"""GitHub engineering-ticket integration.

    config.py      settings and the credential provider (token never stored)
    client.py      REST + GraphQL client; issues, labels, Projects v2
    privacy.py     the private-only gate and the privacy audit
    projects.py    Project board operations in Gary's status vocabulary
    issues.py      issue body, labels
    models.py      typed GitHub results, EngineeringStatus, transitions
    exceptions.py  every failure mode, including GitHubPrivacyError
    setup.py       one-time setup and verification, run by Alex

The client can create and update issues, comment, label, assign, and manage
Project items. It cannot write repository contents or administer the
repository or organization, and it never changes visibility.
"""

from gary.integrations.github.client import GitHubClient
from gary.integrations.github.config import (
    EnvTokenProvider,
    GitHubConfig,
    GitHubCredentialProvider,
    configured,
)
from gary.integrations.github.exceptions import (
    GitHubAuthError,
    GitHubConfigurationError,
    GitHubError,
    GitHubNotFoundError,
    GitHubPermissionError,
    GitHubPrivacyError,
    GitHubRateLimitError,
    GitHubUnavailableError,
    GitHubValidationError,
)
from gary.integrations.github.models import EngineeringStatus
from gary.integrations.github.privacy import PrivacyGate
from gary.integrations.github.projects import ProjectBoard

__all__ = [
    "EngineeringStatus",
    "EnvTokenProvider",
    "GitHubAuthError",
    "GitHubClient",
    "GitHubConfig",
    "GitHubConfigurationError",
    "GitHubCredentialProvider",
    "GitHubError",
    "GitHubNotFoundError",
    "GitHubPermissionError",
    "GitHubPrivacyError",
    "GitHubRateLimitError",
    "GitHubUnavailableError",
    "GitHubValidationError",
    "PrivacyGate",
    "ProjectBoard",
    "configured",
]
