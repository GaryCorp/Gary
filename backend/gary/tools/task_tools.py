from gary.models.common import validate_request
from gary.models.task import (
    CompleteTaskRequest,
    CreateTaskRequest,
    DependencyRequest,
    GetTaskRequest,
    ListTasksRequest,
    UpdateTaskRequest,
)
from gary.tools.base import (
    Tool,
    ToolContext,
    integer,
    obj,
    present,
    priority,
    run_sync,
    string,
    timestamp,
)

TASK_BRIEF_FIELDS = (
    "id",
    "project_id",
    "title",
    "status",
    "priority",
    "estimated_minutes",
    "deadline",
    "earliest_start",
    "scheduled_start",
    "scheduled_end",
)


def brief(task: dict) -> dict:
    return {key: task[key] for key in TASK_BRIEF_FIELDS}


async def task_create(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(CreateTaskRequest, args)
    task = await run_sync(ctx.gary.tasks.create_task, request)
    return {"task": present(brief(task), ctx)}


async def task_update(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(UpdateTaskRequest, args)
    task = await run_sync(ctx.gary.tasks.update_task, request)
    return {"task": present(brief(task), ctx)}


async def task_complete(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(CompleteTaskRequest, args)
    task = await run_sync(ctx.gary.tasks.complete_task, request)
    return {
        "task": present(brief(task), ctx),
        "already_completed": task["already_completed"],
        "now_unblocked": task["unblocked_tasks"],
    }


async def task_list(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(ListTasksRequest, args)
    tasks = await run_sync(ctx.gary.tasks.list_tasks, request.project_id, request.status)
    return {"count": len(tasks), "tasks": present([brief(t) for t in tasks], ctx)}


async def task_get(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(GetTaskRequest, args)
    task = await run_sync(ctx.gary.tasks.get_task, request.task_id)
    return {"task": present(task, ctx)}


async def task_add_dependency(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(DependencyRequest, args)
    return await run_sync(ctx.gary.tasks.add_dependency, request)


async def task_remove_dependency(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(DependencyRequest, args)
    return await run_sync(ctx.gary.tasks.remove_dependency, request)


TOOLS = [
    Tool(
        "task_create",
        "Create a task: one executable unit of work, optionally in a project.",
        obj(
            {
                "project_id": string("project_id this task belongs to, if any."),
                "title": string("Short task title, e.g. Film demo."),
                "description": string("Optional details."),
                "priority": priority(),
                "estimated_minutes": integer("Estimated effort in minutes.", 0),
                "deadline": timestamp("When it must be done."),
                "earliest_start": timestamp("Do not start before this time."),
            },
            ("title",),
        ),
        task_create,
    ),
    Tool(
        "task_update",
        "Change a task. Only pass fields that change; pass null to clear "
        "deadline, earliest_start, description, or project_id. To finish a task "
        "use task_complete; to put it on the calendar use action_propose "
        "schedule_task.",
        obj(
            {
                "task_id": string("task_id of the task."),
                "project_id": string("Move to this project, or null.", nullable=True),
                "title": string("New title."),
                "description": string("New description, or null.", nullable=True),
                "status": string(
                    "New status.",
                    ["todo", "in_progress", "blocked", "waiting", "cancelled"],
                ),
                "priority": priority(),
                "estimated_minutes": integer("Estimated minutes.", 0, nullable=True),
                "actual_minutes": integer("Minutes actually spent.", 0, nullable=True),
                "deadline": timestamp("New deadline, or null.", nullable=True),
                "earliest_start": timestamp("New earliest start, or null.", nullable=True),
            },
            ("task_id",),
        ),
        task_update,
    ),
    Tool(
        "task_complete",
        "Mark a task completed when the user says it is done. Also completes its "
        "pending follow-ups and reports tasks that are now unblocked.",
        obj(
            {
                "task_id": string("task_id of the task."),
                "actual_minutes": integer("Minutes actually spent, if the user said.", 0),
            },
            ("task_id",),
        ),
        task_complete,
    ),
    Tool(
        "task_list",
        "List tasks, optionally for one project. status open (default) means "
        "anything not completed or cancelled.",
        obj(
            {
                "project_id": string("Only tasks in this project."),
                "status": string(
                    "Filter by status.",
                    [
                        "open",
                        "todo",
                        "scheduled",
                        "in_progress",
                        "blocked",
                        "waiting",
                        "completed",
                        "cancelled",
                    ],
                ),
            }
        ),
        task_list,
    ),
    Tool(
        "task_get",
        "Get one task with what it depends on, what it blocks, and whether it is "
        "ready to work on.",
        obj({"task_id": string("task_id of the task.")}, ("task_id",)),
        task_get,
    ),
    Tool(
        "task_add_dependency",
        "Record that one task cannot start until another is completed, e.g. Edit "
        "video depends on Film video. Circular dependencies are rejected.",
        obj(
            {
                "task_id": string("The task that has to wait."),
                "depends_on_task_id": string("The task that must be completed first."),
            },
            ("task_id", "depends_on_task_id"),
        ),
        task_add_dependency,
    ),
    Tool(
        "task_remove_dependency",
        "Remove a dependency between two tasks.",
        obj(
            {
                "task_id": string("The task that was waiting."),
                "depends_on_task_id": string("The task it no longer waits for."),
            },
            ("task_id", "depends_on_task_id"),
        ),
        task_remove_dependency,
    ),
]
