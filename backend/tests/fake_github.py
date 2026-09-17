"""An in-memory GitHub: REST issues/labels and the Projects v2 GraphQL calls
this integration uses. Unit tests never reach the network and never create a
real issue.
"""

import json
import re

from gary.integrations.github.client import Response


class FakeGitHub:
    def __init__(
        self,
        private: bool = True,
        project_public: bool = False,
        assignable: tuple[str, ...] = ("alex",),
        status_options: tuple[str, ...] = (
            "Backlog",
            "Ready",
            "In Progress",
            "Review",
            "Security Review",
            "Done",
        ),
        owner: str = "garycorp",
        repository: str = "gary",
    ):
        self.private = private
        self.project_public = project_public
        self.assignable = set(assignable)
        self.owner = owner
        self.repository = repository
        # What the API reports as the owner, when a test needs it to differ.
        self.reported_owner = owner
        self.has_issues = True

        self.issues: dict[int, dict] = {}
        self.comments: list[dict] = []
        self.labels: dict[str, str] = {}
        self.items: dict[str, dict] = {}  # item id -> {issue node id, fields}
        self.status_options = {name: f"opt-{i}" for i, name in enumerate(status_options)}
        self.extra_fields: dict[str, dict] = {}
        self.project_id = "PVT_project"
        self.project_number = 7
        self.project_title = "GaryCorp Engineering"

        self.requests: list[tuple[str, str]] = []
        self.auth_headers: list[str] = []
        self.fail: dict[str, tuple[int, str]] = {}  # key -> (status, message)
        self.network_error_on: set[str] = set()
        self.calls: dict[str, int] = {}

    # -------------------------------------------------------------- helpers

    def _count(self, key: str) -> int:
        self.calls[key] = self.calls.get(key, 0) + 1
        return self.calls[key]

    def _maybe_fail(self, key: str):
        if key in self.network_error_on:
            from gary.integrations.github.exceptions import GitHubUnavailableError

            raise GitHubUnavailableError("Could not reach GitHub: simulated network failure")
        failure = self.fail.get(key)
        if failure:
            status, message = failure
            return Response(status, {}, json.dumps({"message": message}))
        return None

    def issue_payload(self, issue: dict) -> dict:
        return {
            "number": issue["number"],
            "node_id": issue["node_id"],
            "title": issue["title"],
            "body": issue["body"],
            "state": issue["state"],
            "state_reason": issue.get("state_reason"),
            "html_url": f"https://github.com/{self.owner}/{self.repository}/issues/{issue['number']}",
            "assignees": [{"login": login} for login in issue["assignees"]],
            "labels": [{"name": name} for name in issue["labels"]],
        }

    def project_payload(self) -> dict:
        return {
            "id": self.project_id,
            "number": self.project_number,
            "title": self.project_title,
            "public": self.project_public,
            "url": f"https://github.com/orgs/{self.owner}/projects/{self.project_number}",
            "owner": {"__typename": "Organization", "login": self.owner},
        }

    # ------------------------------------------------------------ transport

    async def request(self, method, url, headers, body, timeout) -> Response:
        self.requests.append((method, url))
        self.auth_headers.append(headers.get("Authorization", ""))
        payload = json.loads(body.decode()) if body else None
        if url.endswith("/graphql"):
            return self._graphql(payload)
        return self._rest(method, url, payload)

    # ----------------------------------------------------------------- REST

    def _rest(self, method: str, url: str, payload: dict | None) -> Response:
        path = url.split("api.github.com", 1)[-1]
        repo_prefix = f"/repos/{self.owner}/{self.repository}"

        if path == repo_prefix and method == "GET":
            self._count("get_repository")
            failed = self._maybe_fail("get_repository")
            if failed:
                return failed
            return self._ok(
                {
                    "name": self.repository,
                    "node_id": "R_repo",
                    "private": self.private,
                    "has_issues": self.has_issues,
                    "owner": {"login": self.reported_owner},
                    "html_url": f"https://github.com/{self.owner}/{self.repository}",
                }
            )

        if path == f"{repo_prefix}/issues" and method == "POST":
            self._count("create_issue")
            failed = self._maybe_fail("create_issue")
            if failed:
                return failed
            number = len(self.issues) + 1
            issue = {
                "number": number,
                "node_id": f"I_issue{number}",
                "title": payload["title"],
                "body": payload["body"],
                "state": "open",
                "assignees": [a for a in payload.get("assignees", []) if a in self.assignable],
                "labels": list(payload.get("labels", [])),
            }
            self.issues[number] = issue
            return self._ok(self.issue_payload(issue), 201)

        match = re.match(rf"^{re.escape(repo_prefix)}/issues/(\d+)$", path)
        if match:
            issue = self.issues.get(int(match.group(1)))
            if issue is None:
                return Response(404, {}, json.dumps({"message": "Not Found"}))
            if method == "GET":
                failed = self._maybe_fail("get_issue")
                if failed:
                    return failed
                return self._ok(self.issue_payload(issue))
            if method == "PATCH":
                failed = self._maybe_fail("update_issue")
                if failed:
                    return failed
                issue.update({k: v for k, v in (payload or {}).items()})
                return self._ok(self.issue_payload(issue))

        match = re.match(rf"^{re.escape(repo_prefix)}/issues/(\d+)/assignees$", path)
        if match and method == "POST":
            self._count("assign_issue")
            failed = self._maybe_fail("assign_issue")
            if failed:
                return failed
            issue = self.issues[int(match.group(1))]
            for login in payload.get("assignees", []):
                if login in self.assignable and login not in issue["assignees"]:
                    issue["assignees"].append(login)
            return self._ok(self.issue_payload(issue), 201)

        match = re.match(rf"^{re.escape(repo_prefix)}/issues/(\d+)/comments$", path)
        if match and method == "POST":
            failed = self._maybe_fail("add_comment")
            if failed:
                return failed
            number = int(match.group(1))
            self.comments.append({"issue": number, "body": payload["body"]})
            return self._ok(
                {"id": len(self.comments), "html_url": f"https://github.com/c/{len(self.comments)}"},
                201,
            )

        match = re.match(rf"^{re.escape(repo_prefix)}/issues/(\d+)/labels$", path)
        if match and method == "POST":
            issue = self.issues[int(match.group(1))]
            for name in payload.get("labels", []):
                if name not in issue["labels"]:
                    issue["labels"].append(name)
            return self._ok([{"name": name} for name in issue["labels"]])

        match = re.match(rf"^{re.escape(repo_prefix)}/issues/(\d+)/labels/(.+)$", path)
        if match and method == "DELETE":
            issue = self.issues[int(match.group(1))]
            label = match.group(2)
            if label not in issue["labels"]:
                return Response(404, {}, json.dumps({"message": "Label does not exist"}))
            issue["labels"].remove(label)
            return self._ok([{"name": name} for name in issue["labels"]])

        if path.startswith(f"{repo_prefix}/labels") and method == "GET":
            self._count("list_labels")
            return self._ok([{"name": name} for name in self.labels])

        if path == f"{repo_prefix}/labels" and method == "POST":
            self._count("create_label")
            failed = self._maybe_fail("create_label")
            if failed:
                return failed
            name = payload["name"]
            if name in self.labels:
                return Response(422, {}, json.dumps({"message": "Validation Failed",
                                                     "errors": [{"code": "already_exists"}]}))
            self.labels[name] = payload.get("color", "")
            return self._ok({"name": name}, 201)

        match = re.match(rf"^{re.escape(repo_prefix)}/assignees/(.+)$", path)
        if match and method == "GET":
            return Response(204 if match.group(1) in self.assignable else 404, {}, "")

        return Response(404, {}, json.dumps({"message": f"Unhandled {method} {path}"}))

    @staticmethod
    def _ok(payload, status: int = 200) -> Response:
        return Response(status, {"Content-Type": "application/json"}, json.dumps(payload))

    # -------------------------------------------------------------- GraphQL

    def _graphql(self, payload: dict) -> Response:
        query = payload["query"]
        variables = payload.get("variables", {})

        if "projectV2(number:" in query.replace(" ", "") or "projectV2(number: $number)" in query:
            self._count("get_project")
            failed = self._maybe_fail("get_project")
            if failed:
                return failed
            if variables.get("number") != self.project_number:
                return self._data({"organization": {"projectV2": None}, "user": None})
            return self._data({"organization": {"projectV2": self.project_payload()}, "user": None})

        if "fields(first: 50)" in query:
            self._count("get_project_fields")
            failed = self._maybe_fail("get_project_fields")
            if failed:
                return failed
            nodes = [
                {
                    "id": "F_status",
                    "name": "Status",
                    "dataType": "SINGLE_SELECT",
                    "options": [{"id": oid, "name": name} for name, oid in self.status_options.items()],
                }
            ]
            for name, field in self.extra_fields.items():
                nodes.append({"id": field["id"], "name": name, "dataType": field["dataType"],
                              "options": [{"id": o, "name": n} for n, o in field.get("options", {}).items()]})
            return self._data({"node": {"fields": {"nodes": nodes}}})

        if "addProjectV2ItemById" in query:
            self._count("add_item")
            failed = self._maybe_fail("add_item")
            if failed:
                return failed
            content = variables["content"]
            for item_id, item in self.items.items():
                if item["content"] == content:
                    return self._data({"addProjectV2ItemById": {"item": {"id": item_id}}})
            item_id = f"PVTI_{len(self.items) + 1}"
            self.items[item_id] = {"content": content, "fields": {}}
            return self._data({"addProjectV2ItemById": {"item": {"id": item_id}}})

        if "updateProjectV2ItemFieldValue" in query:
            self._count("update_field")
            failed = self._maybe_fail("update_field")
            if failed:
                return failed
            item = self.items[variables["item"]]
            value = variables["value"]
            field_name = "Status" if variables["field"] == "F_status" else next(
                (n for n, f in self.extra_fields.items() if f["id"] == variables["field"]), "?"
            )
            if "singleSelectOptionId" in value:
                option = value["singleSelectOptionId"]
                names = {**{v: k for k, v in self.status_options.items()}}
                for name, field in self.extra_fields.items():
                    names.update({v: k for k, v in field.get("options", {}).items()})
                item["fields"][field_name] = names.get(option, option)
            else:
                item["fields"][field_name] = next(iter(value.values()))
            return self._data(
                {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": variables["item"]}}}
            )

        if "projectItems(first: 20)" in query:
            self._count("find_item")
            content = variables["content"]
            nodes = [
                {"id": item_id, "project": {"id": self.project_id}}
                for item_id, item in self.items.items()
                if item["content"] == content
            ]
            return self._data({"node": {"projectItems": {"nodes": nodes}}})

        if "fieldValues(first: 20)" in query:
            self._count("get_item")
            failed = self._maybe_fail("get_item")
            if failed:
                return failed
            item = self.items.get(variables["item"])
            if item is None:
                return self._data({"node": None})
            nodes = [
                {"name": value, "field": {"name": name}} for name, value in item["fields"].items()
            ]
            return self._data({"node": {"id": variables["item"], "fieldValues": {"nodes": nodes}}})

        if "createProjectV2" in query:
            self._count("create_project")
            self.project_number = 9
            return self._data({"createProjectV2": {"projectV2": self.project_payload()}})

        if "organization(login: $owner) { id }" in query.replace("\n", " ") or "{ id }" in query:
            return self._data({"organization": {"id": "O_owner"}, "user": None})

        return Response(400, {}, json.dumps({"message": f"Unhandled GraphQL: {query[:80]}"}))

    @staticmethod
    def _data(data: dict) -> Response:
        return Response(200, {"Content-Type": "application/json"}, json.dumps({"data": data}))

    # --------------------------------------------------------------- probes

    def status_of(self, item_id: str) -> str | None:
        return self.items[item_id]["fields"].get("Status")

    def issue_body(self, number: int = 1) -> str:
        return self.issues[number]["body"]
