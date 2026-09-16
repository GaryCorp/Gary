-- Gary Chief of Staff: initial schema.
-- Timestamps are ISO 8601 strings normalized to UTC (see gary/timeutil.py).

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,

    name TEXT NOT NULL,
    objective TEXT NOT NULL,

    status TEXT NOT NULL DEFAULT 'active'
        CHECK (
            status IN (
                'planned',
                'active',
                'blocked',
                'completed',
                'cancelled',
                'archived'
            )
        ),

    priority INTEGER NOT NULL DEFAULT 5
        CHECK (priority BETWEEN 1 AND 10),

    deadline TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,

    metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,

    project_id TEXT,

    title TEXT NOT NULL,
    description TEXT,

    status TEXT NOT NULL DEFAULT 'todo'
        CHECK (
            status IN (
                'todo',
                'scheduled',
                'in_progress',
                'blocked',
                'waiting',
                'completed',
                'cancelled'
            )
        ),

    priority INTEGER NOT NULL DEFAULT 5
        CHECK (priority BETWEEN 1 AND 10),

    estimated_minutes INTEGER
        CHECK (
            estimated_minutes IS NULL
            OR estimated_minutes >= 0
        ),

    actual_minutes INTEGER
        CHECK (
            actual_minutes IS NULL
            OR actual_minutes >= 0
        ),

    deadline TEXT,

    earliest_start TEXT,

    scheduled_start TEXT,
    scheduled_end TEXT,

    calendar_event_id TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    started_at TEXT,
    completed_at TEXT,

    created_by TEXT NOT NULL DEFAULT 'gary',

    metadata_json TEXT,

    FOREIGN KEY(project_id)
        REFERENCES projects(id)
        ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id TEXT NOT NULL,
    depends_on_task_id TEXT NOT NULL,

    created_at TEXT NOT NULL,

    PRIMARY KEY (
        task_id,
        depends_on_task_id
    ),

    FOREIGN KEY(task_id)
        REFERENCES tasks(id)
        ON DELETE CASCADE,

    FOREIGN KEY(depends_on_task_id)
        REFERENCES tasks(id)
        ON DELETE CASCADE,

    CHECK(task_id != depends_on_task_id)
);

CREATE TABLE IF NOT EXISTS followups (
    id TEXT PRIMARY KEY,

    project_id TEXT,
    task_id TEXT,

    title TEXT NOT NULL,
    description TEXT,

    due_at TEXT NOT NULL,

    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (
            status IN (
                'pending',
                'completed',
                'cancelled'
            )
        ),

    priority INTEGER NOT NULL DEFAULT 5
        CHECK(priority BETWEEN 1 AND 10),

    created_at TEXT NOT NULL,
    completed_at TEXT,

    FOREIGN KEY(project_id)
        REFERENCES projects(id)
        ON DELETE CASCADE,

    FOREIGN KEY(task_id)
        REFERENCES tasks(id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS commitments (
    id TEXT PRIMARY KEY,

    project_id TEXT,
    task_id TEXT,

    description TEXT NOT NULL,

    committed_to TEXT,

    deadline TEXT,

    status TEXT NOT NULL DEFAULT 'open'
        CHECK (
            status IN (
                'open',
                'fulfilled',
                'missed',
                'cancelled'
            )
        ),

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    FOREIGN KEY(project_id)
        REFERENCES projects(id)
        ON DELETE SET NULL,

    FOREIGN KEY(task_id)
        REFERENCES tasks(id)
        ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,

    action_type TEXT NOT NULL,

    summary TEXT NOT NULL,

    payload_json TEXT NOT NULL,

    reason TEXT,

    risk_level TEXT NOT NULL
        CHECK (
            risk_level IN (
                'green',
                'yellow',
                'red'
            )
        ),

    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (
            status IN (
                'pending',
                'approved',
                'rejected',
                'expired',
                'cancelled'
            )
        ),

    requested_by TEXT NOT NULL DEFAULT 'gary',

    created_at TEXT NOT NULL,
    resolved_at TEXT,

    resolution_note TEXT
);

CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,

    action_type TEXT NOT NULL,

    project_id TEXT,
    task_id TEXT,

    payload_json TEXT NOT NULL,

    reason TEXT,

    risk_level TEXT NOT NULL
        CHECK (
            risk_level IN (
                'green',
                'yellow',
                'red'
            )
        ),

    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK (
            status IN (
                'proposed',
                'awaiting_approval',
                'approved',
                'executing',
                'succeeded',
                'failed',
                'rejected',
                'cancelled'
            )
        ),

    approval_id TEXT,

    created_at TEXT NOT NULL,
    executed_at TEXT,

    result_json TEXT,
    error_message TEXT,

    FOREIGN KEY(project_id)
        REFERENCES projects(id)
        ON DELETE SET NULL,

    FOREIGN KEY(task_id)
        REFERENCES tasks(id)
        ON DELETE SET NULL,

    FOREIGN KEY(approval_id)
        REFERENCES approvals(id)
        ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    timestamp TEXT NOT NULL,

    actor TEXT NOT NULL,

    event_type TEXT NOT NULL,

    entity_type TEXT,
    entity_id TEXT,

    summary TEXT NOT NULL,

    details_json TEXT
);

-- The audit log is append-only, enforced by the database itself.
CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TABLE IF NOT EXISTS planning_runs (
    id TEXT PRIMARY KEY,

    started_at TEXT NOT NULL,
    completed_at TEXT,

    planning_type TEXT NOT NULL
        CHECK (
            planning_type IN (
                'morning',
                'midday',
                'evening',
                'event_triggered',
                'manual'
            )
        ),

    input_summary TEXT,

    plan_json TEXT,

    status TEXT NOT NULL
        CHECK (
            status IN (
                'running',
                'completed',
                'failed'
            )
        ),

    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status_deadline ON tasks(status, deadline);
CREATE INDEX IF NOT EXISTS idx_task_dependencies_depends_on
    ON task_dependencies(depends_on_task_id);
CREATE INDEX IF NOT EXISTS idx_followups_status_due ON followups(status, due_at);
CREATE INDEX IF NOT EXISTS idx_followups_task ON followups(task_id);
CREATE INDEX IF NOT EXISTS idx_commitments_status ON commitments(status);
CREATE INDEX IF NOT EXISTS idx_commitments_task ON commitments(task_id);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);
CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);
CREATE INDEX IF NOT EXISTS idx_actions_approval ON actions(approval_id);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_log(event_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_planning_runs_started ON planning_runs(started_at);
