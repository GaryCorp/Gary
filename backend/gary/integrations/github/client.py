"""The GitHub API client: the only place in GaryCorp that speaks to GitHub.

REST covers issues, labels, assignment, and repository metadata. Projects v2
is GraphQL only (Projects Classic is retired and is not used). Transport is
injectable so tests never touch the network.

The client can create and update issues, comment, label, assign, and manage
Project items. It deliberately has no code for repository administration:
no visibility changes, deletion, renaming, transfers, collaborators, branch
protection, Actions secrets, workflows, releases, packages, or Pages. It also
never writes repository contents, so Gary cannot change source code.
"""

import asyncio
import json
import logging
import random
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol

from gary.integrations.github.config import GitHubConfig, GitHubCredentialProvider
from gary.integrations.github.exceptions import (
    GitHubAuthError,
    GitHubConflictError,
    GitHubError,
    GitHubNotFoundError,
    GitHubPermissionError,
    GitHubRateLimitError,
    GitHubUnavailableError,
    GitHubValidationError,
)
from gary.integrations.github.models import Issue, Project, ProjectField, ProjectItem, Repository

logger = logging.getLogger("gary.github")

USER_AGENT = "GaryCorp-Engineering/1.0"
ACCEPT = "application/vnd.github+json"
API_VERSION = "2022-11-28"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_BACKOFF_SECONDS = 20.0
# A Retry-After longer than this is honored by giving up, not by sleeping.
MAX_RETRY_AFTER_SECONDS = 60.0


class Response:
    def __init__(self, status: int, headers: dict[str, str], body: str):
        self.status = status
        self.headers = {key.lower(): value for key, value in headers.items()}
        self.body = body

    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return json.loads(self.body)
        except json.JSONDecodeError:
            raise GitHubUnavailableError("GitHub returned a response that was not JSON") from None


class Transport(Protocol):
    async def request(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
    ) -> Response: ...


class UrllibTransport:
    """Blocking urllib in a worker thread, like Gary's other API clients."""

    async def request(self, method, url, headers, body, timeout) -> Response:
        return await asyncio.to_thread(self._request, method, url, headers, body, timeout)

    def _request(self, method, url, headers, body, timeout) -> Response:
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return Response(response.status, dict(response.headers), response.read().decode())
        except urllib.error.HTTPError as exc:
            return Response(exc.code, dict(exc.headers or {}), exc.read().decode(errors="replace"))
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            # The reason may name the host but never the credential.
            raise GitHubUnavailableError(f"Could not reach GitHub: {exc}") from exc


def _redacted(headers: dict[str, str]) -> dict[str, str]:
    """Headers safe to log: the credential is replaced, never truncated."""
    return {
        key: ("[redacted]" if key.lower() in ("authorization", "cookie") else value)
        for key, value in headers.items()
    }


def _message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        message = payload.get("message") or fallback
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            details = "; ".join(
                error.get("message") or f"{error.get('field')}: {error.get('code')}"
                for error in errors
                if isinstance(error, dict)
            )
            if details:
                return f"{message} ({details})"
        return str(message)
    return fallback


class GitHubClient:
    def __init__(
        self,
        config: GitHubConfig,
        credentials: GitHubCredentialProvider,
        transport: Transport | None = None,
        sleep=asyncio.sleep,
    ):
        self.config = config
        self._credentials = credentials
        self._transport = transport or UrllibTransport()
        self._sleep = sleep

    # ------------------------------------------------------------- transport

    async def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {await self._credentials.get_token()}",
            "Accept": ACCEPT,
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        }

    async def _send(self, method: str, url: str, body: dict | None) -> Response:
        headers = await self._headers()
        payload = json.dumps(body).encode() if body is not None else None
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._transport.request(
                    method, url, headers, payload, self.config.timeout
                )
            except GitHubUnavailableError:
                if attempt >= self.config.max_attempts:
                    raise
                await self._backoff(attempt)
                continue

            if response.status in RETRY_STATUSES and attempt < self.config.max_attempts:
                await self._backoff(attempt, response)
                continue
            # Only the method, URL, status, and redacted headers are logged.
            logger.debug(
                "GitHub %s %s -> %s (headers %s)",
                method,
                url,
                response.status,
                _redacted(headers),
            )
            return response

    async def _backoff(self, attempt: int, response: Response | None = None) -> None:
        delay = min(MAX_BACKOFF_SECONDS, 2 ** (attempt - 1)) + random.uniform(0, 0.5)
        if response is not None:
            after = response.headers.get("retry-after")
            if after and after.strip().isdigit():
                seconds = float(after.strip())
                if seconds > MAX_RETRY_AFTER_SECONDS:
                    raise GitHubRateLimitError(
                        f"GitHub asked us to wait {int(seconds)} seconds; not retrying now"
                    )
                delay = seconds
        await self._sleep(delay)

    def _raise(self, response: Response, what: str) -> None:
        payload = None
        try:
            payload = response.json()
        except GitHubError:
            payload = None
        message = _message(payload, response.body[:200] or "no details")
        status = response.status

        if status == 401:
            raise GitHubAuthError(f"GitHub rejected the credential while {what}: {message}")
        if status == 403:
            lowered = message.casefold()
            if "rate limit" in lowered or "abuse" in lowered or "secondary" in lowered:
                raise GitHubRateLimitError(f"GitHub rate limit hit while {what}: {message}")
            raise GitHubPermissionError(
                f"The GaryCorp credential lacks permission for {what}: {message}"
            )
        if status == 404:
            raise GitHubNotFoundError(
                f"Not found while {what}: {message}. The resource may not exist, "
                "or the credential may not have access to it."
            )
        if status == 409:
            raise GitHubConflictError(f"Conflict while {what}: {message}")
        if status == 422:
            raise GitHubValidationError(f"GitHub rejected the request while {what}: {message}")
        if status == 429:
            raise GitHubRateLimitError(f"GitHub rate limit hit while {what}: {message}")
        if status >= 500:
            raise GitHubUnavailableError(f"GitHub failed while {what}: HTTP {status}")
        raise GitHubError(f"Unexpected GitHub response while {what}: HTTP {status} {message}")

    async def rest(self, method: str, path: str, body: dict | None = None, *, what: str) -> Any:
        response = await self._send(method, f"{self.config.rest_url}{path}", body)
        if response.status >= 400:
            self._raise(response, what)
        return response.json()

    async def graphql(self, query: str, variables: dict, *, what: str) -> dict:
        response = await self._send(
            "POST", self.config.graphql_url, {"query": query, "variables": variables}
        )
        if response.status >= 400:
            self._raise(response, what)
        payload = response.json() or {}
        errors = payload.get("errors")
        if errors:
            first = errors[0] if isinstance(errors, list) and errors else {}
            message = first.get("message", "unknown GraphQL error")
            error_type = (first.get("type") or "").upper()
            if error_type in ("FORBIDDEN", "INSUFFICIENT_SCOPES"):
                raise GitHubPermissionError(
                    f"The GaryCorp credential lacks permission for {what}: {message}"
                )
            if error_type == "NOT_FOUND":
                raise GitHubNotFoundError(f"Not found while {what}: {message}")
            if error_type == "UNAUTHORIZED":
                raise GitHubAuthError(f"GitHub rejected the credential while {what}: {message}")
            if "rate limit" in message.casefold():
                raise GitHubRateLimitError(f"GitHub rate limit hit while {what}: {message}")
            raise GitHubError(f"GitHub rejected the request while {what}: {message}")
        data = payload.get("data")
        if data is None:
            raise GitHubError(f"GitHub returned no data while {what}")
        return data

    # ---------------------------------------------------------- repositories

    @property
    def _repo_path(self) -> str:
        return f"/repos/{self.config.owner}/{self.config.repository}"

    async def get_repository(self) -> Repository:
        data = await self.rest("GET", self._repo_path, what="reading the repository")
        return Repository(
            owner=(data.get("owner") or {}).get("login", self.config.owner),
            name=data.get("name", self.config.repository),
            node_id=data.get("node_id", ""),
            private=bool(data.get("private")),
            has_issues=bool(data.get("has_issues", True)),
            html_url=data.get("html_url", ""),
        )

    # ---------------------------------------------------------------- issues

    @staticmethod
    def _issue(data: dict) -> Issue:
        return Issue(
            number=data["number"],
            node_id=data["node_id"],
            title=data.get("title", ""),
            state=data.get("state", "open"),
            state_reason=data.get("state_reason"),
            html_url=data.get("html_url", ""),
            assignees=[a.get("login", "") for a in data.get("assignees") or []],
            labels=[
                label.get("name", "") if isinstance(label, dict) else str(label)
                for label in data.get("labels") or []
            ],
        )

    async def create_issue(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
    ) -> Issue:
        payload: dict = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        if assignees:
            payload["assignees"] = assignees
        data = await self.rest(
            "POST", f"{self._repo_path}/issues", payload, what="creating the issue"
        )
        return self._issue(data)

    async def get_issue(self, number: int) -> Issue:
        data = await self.rest(
            "GET", f"{self._repo_path}/issues/{number}", what=f"reading issue #{number}"
        )
        return self._issue(data)

    async def update_issue(self, number: int, **changes) -> Issue:
        allowed = {"title", "body", "state", "state_reason", "labels", "assignees", "milestone"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"Cannot update issue fields: {sorted(unknown)}")
        data = await self.rest(
            "PATCH", f"{self._repo_path}/issues/{number}", changes, what=f"updating issue #{number}"
        )
        return self._issue(data)

    async def add_issue_comment(self, number: int, body: str) -> dict:
        data = await self.rest(
            "POST",
            f"{self._repo_path}/issues/{number}/comments",
            {"body": body},
            what=f"commenting on issue #{number}",
        )
        return {"id": data.get("id"), "html_url": data.get("html_url", "")}

    async def add_labels(self, number: int, labels: list[str]) -> list[str]:
        data = await self.rest(
            "POST",
            f"{self._repo_path}/issues/{number}/labels",
            {"labels": labels},
            what=f"labelling issue #{number}",
        )
        return [label.get("name", "") for label in data or []]

    async def remove_label(self, number: int, label: str) -> None:
        quoted = urllib.parse.quote(label, safe="")
        try:
            await self.rest(
                "DELETE",
                f"{self._repo_path}/issues/{number}/labels/{quoted}",
                what=f"removing label {label} from issue #{number}",
            )
        except GitHubNotFoundError:
            return  # The label was not on the issue.

    async def list_labels(self) -> list[str]:
        data = await self.rest(
            "GET", f"{self._repo_path}/labels?per_page=100", what="listing labels"
        )
        return [label.get("name", "") for label in data or []]

    async def create_label(self, name: str, color: str, description: str = "") -> bool:
        """Create the label. Returns False when it already existed."""
        try:
            await self.rest(
                "POST",
                f"{self._repo_path}/labels",
                {"name": name, "color": color, "description": description[:100]},
                what=f"creating label {name}",
            )
            return True
        except GitHubValidationError:
            # 422 "already_exists": reuse it rather than duplicating.
            return False

    async def assign_issue(self, number: int, assignees: list[str]) -> Issue:
        data = await self.rest(
            "POST",
            f"{self._repo_path}/issues/{number}/assignees",
            {"assignees": assignees},
            what=f"assigning issue #{number}",
        )
        return self._issue(data)

    async def can_be_assigned(self, username: str) -> bool:
        """True when the repository would accept this user as an assignee."""
        quoted = urllib.parse.quote(username, safe="")
        response = await self._send(
            "GET", f"{self.config.rest_url}{self._repo_path}/assignees/{quoted}", None
        )
        if response.status == 204:
            return True
        if response.status == 404:
            return False
        self._raise(response, f"checking whether {username} can be assigned")
        return False

    # -------------------------------------------------------- projects (v2)

    _PROJECT_FRAGMENT = """
        id
        number
        title
        public
        url
        owner { __typename ... on Organization { login } ... on User { login } }
    """

    async def get_project(self, number: int | None = None) -> Project:
        number = number or self.config.project_number
        query = """
        query($owner: String!, $number: Int!) {
          organization(login: $owner) { projectV2(number: $number) { %s } }
          user(login: $owner) { projectV2(number: $number) { %s } }
        }
        """ % (self._PROJECT_FRAGMENT, self._PROJECT_FRAGMENT)
        data = await self.graphql(
            query, {"owner": self.config.owner, "number": number}, what="reading the Project"
        )
        node = (data.get("organization") or {}).get("projectV2") or (
            data.get("user") or {}
        ).get("projectV2")
        if not node:
            raise GitHubNotFoundError(
                f"No Project number {number} owned by {self.config.owner} is visible to "
                "the GaryCorp credential. Check GITHUB_PROJECT_NUMBER and that the "
                "credential has organization Projects access."
            )
        return Project(
            id=node["id"],
            number=node["number"],
            title=node.get("title", ""),
            public=bool(node.get("public")),
            owner_login=(node.get("owner") or {}).get("login", self.config.owner),
            url=node.get("url", ""),
        )

    async def get_project_fields(self, project_id: str) -> dict[str, ProjectField]:
        query = """
        query($project: ID!) {
          node(id: $project) {
            ... on ProjectV2 {
              fields(first: 50) {
                nodes {
                  ... on ProjectV2FieldCommon { id name dataType }
                  ... on ProjectV2SingleSelectField {
                    id name dataType options { id name }
                  }
                }
              }
            }
          }
        }
        """
        data = await self.graphql(query, {"project": project_id}, what="reading Project fields")
        nodes = ((data.get("node") or {}).get("fields") or {}).get("nodes") or []
        fields: dict[str, ProjectField] = {}
        for node in nodes:
            if not node or not node.get("id"):
                continue
            fields[node["name"]] = ProjectField(
                id=node["id"],
                name=node["name"],
                data_type=node.get("dataType", ""),
                options={o["name"]: o["id"] for o in node.get("options") or []},
            )
        return fields

    async def add_issue_to_project(self, project_id: str, issue_node_id: str) -> str:
        """Add the issue and return the Project item id. Adding twice returns
        the existing item, so retries are safe."""
        query = """
        mutation($project: ID!, $content: ID!) {
          addProjectV2ItemById(input: {projectId: $project, contentId: $content}) {
            item { id }
          }
        }
        """
        data = await self.graphql(
            query,
            {"project": project_id, "content": issue_node_id},
            what="adding the issue to the Project",
        )
        item = ((data.get("addProjectV2ItemById") or {}).get("item") or {}).get("id")
        if not item:
            raise GitHubError("GitHub did not return a Project item id")
        return item

    async def update_project_field(
        self, project_id: str, item_id: str, field_id: str, value: dict
    ) -> str:
        query = """
        mutation($project: ID!, $item: ID!, $field: ID!, $value: ProjectV2FieldValue!) {
          updateProjectV2ItemFieldValue(
            input: {projectId: $project, itemId: $item, fieldId: $field, value: $value}
          ) { projectV2Item { id } }
        }
        """
        data = await self.graphql(
            query,
            {"project": project_id, "item": item_id, "field": field_id, "value": value},
            what="updating a Project field",
        )
        updated = (
            (data.get("updateProjectV2ItemFieldValue") or {}).get("projectV2Item") or {}
        ).get("id")
        if not updated:
            raise GitHubError("GitHub did not confirm the Project field update")
        return updated

    async def get_project_item(self, item_id: str) -> ProjectItem:
        query = """
        query($item: ID!) {
          node(id: $item) {
            ... on ProjectV2Item {
              id
              fieldValues(first: 20) {
                nodes {
                  ... on ProjectV2ItemFieldSingleSelectValue {
                    name field { ... on ProjectV2FieldCommon { name } }
                  }
                }
              }
            }
          }
        }
        """
        data = await self.graphql(query, {"item": item_id}, what="reading the Project item")
        node = data.get("node")
        if not node:
            raise GitHubNotFoundError("That Project item no longer exists")
        values = {}
        for value in ((node.get("fieldValues") or {}).get("nodes") or []):
            name = (value or {}).get("name")
            field = ((value or {}).get("field") or {}).get("name")
            if name and field:
                values[field] = name
        return ProjectItem(id=node["id"], field_values=values)

    async def find_project_item(self, project_id: str, issue_node_id: str) -> str | None:
        """The Project item for this issue, if the issue is already on the board."""
        query = """
        query($content: ID!) {
          node(id: $content) {
            ... on Issue {
              projectItems(first: 20) {
                nodes { id project { id } }
              }
            }
          }
        }
        """
        data = await self.graphql(
            query, {"content": issue_node_id}, what="looking for the issue on the Project"
        )
        nodes = (((data.get("node") or {}).get("projectItems")) or {}).get("nodes") or []
        for item in nodes:
            if ((item or {}).get("project") or {}).get("id") == project_id:
                return item["id"]
        return None

    # ------------------------------------------------ setup-only operations

    async def get_owner_id(self) -> tuple[str, str]:
        """The owner's node id and type, for creating the Project in setup."""
        query = """
        query($owner: String!) {
          organization(login: $owner) { id }
          user(login: $owner) { id }
        }
        """
        data = await self.graphql(query, {"owner": self.config.owner}, what="reading the owner")
        organization = (data.get("organization") or {}).get("id")
        if organization:
            return organization, "organization"
        user = (data.get("user") or {}).get("id")
        if user:
            return user, "user"
        raise GitHubNotFoundError(f"No GitHub owner named {self.config.owner} is visible")

    async def create_project(self, owner_id: str, title: str) -> Project:
        """Create a Project during explicit setup only.

        Projects v2 are created private; the caller verifies that and refuses
        to continue if GitHub ever returns a public one. Nothing here changes
        the visibility of an existing Project.
        """
        query = """
        mutation($owner: ID!, $title: String!) {
          createProjectV2(input: {ownerId: $owner, title: $title}) {
            projectV2 { %s }
          }
        }
        """ % self._PROJECT_FRAGMENT
        data = await self.graphql(
            query, {"owner": owner_id, "title": title}, what="creating the Project"
        )
        node = (data.get("createProjectV2") or {}).get("projectV2")
        if not node:
            raise GitHubError("GitHub did not return the new Project")
        return Project(
            id=node["id"],
            number=node["number"],
            title=node.get("title", title),
            public=bool(node.get("public")),
            owner_login=(node.get("owner") or {}).get("login", self.config.owner),
            url=node.get("url", ""),
        )

    async def add_status_option(
        self, project_id: str, field: ProjectField, option_names: list[str]
    ) -> ProjectField:
        """Add missing options to an existing single-select field (setup only)."""
        query = """
        mutation($field: ID!, $options: [ProjectV2SingleSelectFieldOptionInput!]!) {
          updateProjectV2Field(
            input: {fieldId: $field, singleSelectOptions: $options}
          ) {
            projectV2Field {
              ... on ProjectV2SingleSelectField { id name options { id name } }
            }
          }
        }
        """
        options = [
            {"name": name, "color": "GRAY", "description": ""}
            for name in list(field.options) + [n for n in option_names if not field.option_id(n)]
        ]
        data = await self.graphql(
            query, {"field": field.id, "options": options}, what="adding Status options"
        )
        node = (data.get("updateProjectV2Field") or {}).get("projectV2Field") or {}
        return ProjectField(
            id=node.get("id", field.id),
            name=node.get("name", field.name),
            data_type=field.data_type,
            options={o["name"]: o["id"] for o in node.get("options") or []},
        )
