"""One-time setup and verification, run by Alex:

    docker compose exec backend python -m gary.integrations.github.setup
    docker compose exec backend python -m gary.integrations.github.setup --create-project

It verifies authentication, that the repository exists and is PRIVATE, finds
(or, only with --create-project, creates) the private Engineering Project,
discovers the Status field and option ids, checks that the configured engineer
can be assigned, and ensures the labels exist.

If anything that must be private is public, it stops and explains. It never
changes visibility: that is Alex's decision, made by hand in GitHub.
"""

import argparse
import asyncio
import sys

from gary.integrations.github.client import GitHubClient
from gary.integrations.github.config import EnvTokenProvider, GitHubConfig
from gary.integrations.github.exceptions import GitHubError, GitHubPrivacyError
from gary.integrations.github.issues import ensure_labels
from gary.integrations.github.models import LABEL_COLORS, REQUIRED_STATUS_OPTIONS
from gary.integrations.github.projects import STATUS_FIELD, ProjectBoard

DEFAULT_PROJECT_TITLE = "GaryCorp Engineering"


def line(ok: bool | None, text: str) -> None:
    mark = {True: "ok  ", False: "FAIL", None: "warn"}[ok]
    print(f"[{mark}] {text}")


async def run(create_project: bool, create_labels: bool, project_title: str) -> int:
    import os

    config = GitHubConfig.from_env()
    client = GitHubClient(config, EnvTokenProvider(os.environ.get("GITHUB_TOKEN", "")))

    repository = await client.get_repository()
    line(True, f"Authenticated and found {repository.full_name}")
    if not repository.private:
        line(False, f"{repository.full_name} is PUBLIC")
        print(
            "\nStopping. GaryCorp is proprietary and this integration only works with a "
            "private repository.\nMake it private in GitHub yourself "
            "(Settings > General > Danger Zone > Change visibility), then run setup again.\n"
            "Nothing was created."
        )
        return 2
    line(True, "Repository is private")
    if not repository.has_issues:
        line(False, "Issues are disabled on the repository; enable them in Settings > General")
        return 2

    try:
        project = await client.get_project()
    except GitHubError as exc:
        if not create_project:
            line(False, f"Could not find Project {config.project_number}: {exc}")
            print(
                "\nCreate it in GitHub as a PRIVATE organization Project and set "
                "GITHUB_PROJECT_NUMBER, or re-run with --create-project to create "
                f"{project_title!r} now."
            )
            return 2
        owner_id, owner_type = await client.get_owner_id()
        project = await client.create_project(owner_id, project_title)
        line(True, f"Created {owner_type} Project {project.title!r} (number {project.number})")
        if project.public:
            raise GitHubPrivacyError(
                f"The new Project {project.title!r} came back public. Delete it or make it "
                "private in GitHub; this command will not change visibility."
            )
        print(f"       Set GITHUB_PROJECT_NUMBER={project.number} in .env")

    line(True, f"Found Project {project.title!r} (number {project.number})")
    if project.public:
        line(False, f"Project {project.title!r} is PUBLIC")
        print(
            "\nStopping. GaryCorp issues will not be added to a public Project.\n"
            "Make it private in GitHub (Project > Settings > Manage access), then run setup "
            "again. Nothing was changed."
        )
        return 2
    line(True, "Project is private")
    if project.owner_login.casefold() != config.owner.casefold():
        line(False, f"Project belongs to {project.owner_login}, not GITHUB_OWNER {config.owner}")
        return 2

    board = ProjectBoard(client)
    fields = await board.fields(project.id)
    status_field = fields[STATUS_FIELD]
    line(True, f"Status field found (id {status_field.id})")

    missing = [name for name in REQUIRED_STATUS_OPTIONS if not status_field.option_id(name)]
    if missing:
        line(None, f"Status is missing options: {', '.join(missing)}")
        print(
            "       Add them in the Project (Status field > Edit), in this order:\n"
            f"       {', '.join(REQUIRED_STATUS_OPTIONS)}\n"
            "       Gary uses the options that exist; without them a status change is skipped."
        )
    else:
        line(True, f"Status options present: {', '.join(REQUIRED_STATUS_OPTIONS)}")
    for name, field in fields.items():
        if name != STATUS_FIELD:
            line(True, f"Optional field available: {name} ({field.data_type})")

    assignable = await client.can_be_assigned(config.engineer_username)
    line(
        assignable,
        f"{config.engineer_username} "
        + ("can be assigned issues" if assignable else "CANNOT be assigned issues in this repository"),
    )
    if not assignable:
        print(
            "       Give them access to the private repository, or fix "
            "GITHUB_ENGINEER_USERNAME. Tickets would be created unassigned and flagged."
        )

    wanted = sorted(LABEL_COLORS)
    if create_labels:
        result = await ensure_labels(client, wanted)
        line(True, f"Labels created: {', '.join(result['created']) or 'none'}")
        line(True, f"Labels reused: {', '.join(result['reused']) or 'none'}")
    else:
        existing = {name.casefold() for name in await client.list_labels()}
        absent = [label for label in wanted if label.casefold() not in existing]
        line(not absent or None, f"Labels missing: {', '.join(absent) or 'none'}")
        if absent:
            print("       Re-run with --create-labels, or let ticket creation add them.")

    print("\nConfiguration (no secrets shown):")
    print(f"  GITHUB_OWNER={config.owner}")
    print(f"  GITHUB_REPOSITORY={config.repository}")
    print(f"  GITHUB_PROJECT_NUMBER={project.number}")
    print(f"  GITHUB_PROJECT_ID={project.id}")
    print(f"  GITHUB_ENGINEER_USERNAME={config.engineer_username}")
    print(f"  Repository private: {repository.private}   Project private: {project.private}")
    print("\nSafe to operate: everything required is private.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m gary.integrations.github.setup")
    parser.add_argument(
        "--create-project",
        action="store_true",
        help="create the private Engineering Project if it does not exist",
    )
    parser.add_argument(
        "--create-labels", action="store_true", help="create any missing GaryCorp labels"
    )
    parser.add_argument("--project-title", default=DEFAULT_PROJECT_TITLE)
    args = parser.parse_args(argv)

    try:
        return asyncio.run(run(args.create_project, args.create_labels, args.project_title))
    except GitHubPrivacyError as exc:
        line(False, str(exc))
        print("\nStopping. Visibility must be changed by you, in GitHub.")
        return 2
    except GitHubError as exc:
        line(False, str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
