from gary.models.common import validate_request
from gary.models.project import (
    CreateProjectRequest,
    GetProjectRequest,
    ListProjectsRequest,
    UpdateProjectRequest,
)
from gary.tools.base import (
    Tool,
    ToolContext,
    boolean,
    obj,
    present,
    priority,
    run_sync,
    string,
    timestamp,
)

PROJECT_STATUSES = ["planned", "active", "blocked", "completed", "cancelled", "archived"]


async def project_create(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(CreateProjectRequest, args)
    project = await run_sync(ctx.gary.projects.create_project, request)
    return {"project": present(project, ctx)}


async def project_list(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(ListProjectsRequest, args)
    projects = await run_sync(ctx.gary.projects.list_projects, request.include_closed)
    return {"count": len(projects), "projects": present(projects, ctx)}


async def project_get(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(GetProjectRequest, args)
    project = await run_sync(ctx.gary.projects.get_project, request.project_id)
    return {"project": present(project, ctx)}


async def project_update(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(UpdateProjectRequest, args)
    project = await run_sync(ctx.gary.projects.update_project, request)
    return {"project": present(project, ctx)}


TOOLS = [
    Tool(
        "project_create",
        "Create a project: a larger objective made of tasks, such as publishing "
        "a video. Use when the user states a goal that needs several steps.",
        obj(
            {
                "name": string("Short project name."),
                "objective": string("What done looks like, in one or two sentences."),
                "status": string("Initial status; default active.", ["planned", "active"]),
                "priority": priority(),
                "deadline": timestamp("Project deadline.", nullable=True),
            },
            ("name", "objective"),
        ),
        project_create,
    ),
    Tool(
        "project_list",
        "List projects. By default only planned, active, and blocked projects.",
        obj({"include_closed": boolean("Also include completed, cancelled, and archived.")}),
        project_list,
    ),
    Tool(
        "project_get",
        "Get one project with its tasks, which tasks are ready or blocked, and "
        "its pending follow-ups.",
        obj({"project_id": string("project_id from project_list or project_create.")}, ("project_id",)),
        project_get,
    ),
    Tool(
        "project_update",
        "Change a project's name, objective, status, priority, or deadline. Set "
        "status completed when the user says the project is done. Only pass "
        "fields that change; pass deadline null to clear it.",
        obj(
            {
                "project_id": string("project_id of the project."),
                "name": string("New name."),
                "objective": string("New objective."),
                "status": string("New status.", PROJECT_STATUSES),
                "priority": priority(),
                "deadline": timestamp("New deadline, or null to clear.", nullable=True),
            },
            ("project_id",),
        ),
        project_update,
    ),
]
