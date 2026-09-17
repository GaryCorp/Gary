-- GaryCorp specialist employees: org chart, assignments, management reviews,
-- and agent runs. Agent identity and permissions are defined in code
-- (gary/agents/roster.py); the agents table mirrors identity for the org chart
-- and referential integrity, and is never read for permissions.

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,

    name TEXT NOT NULL,
    title TEXT NOT NULL,
    department TEXT NOT NULL,

    reports_to TEXT,

    is_employee INTEGER NOT NULL DEFAULT 1
        CHECK (is_employee IN (0, 1)),

    active INTEGER NOT NULL DEFAULT 1
        CHECK (active IN (0, 1)),

    updated_at TEXT NOT NULL,

    FOREIGN KEY(reports_to)
        REFERENCES agents(id)
        ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS management_reviews (
    id TEXT PRIMARY KEY,

    topic TEXT NOT NULL,

    requested_by TEXT NOT NULL,

    project_id TEXT,
    task_id TEXT,

    status TEXT NOT NULL DEFAULT 'running'
        CHECK (
            status IN (
                'running',
                'completed',
                'partial',
                'failed'
            )
        ),

    follow_ups_used INTEGER NOT NULL DEFAULT 0
        CHECK (follow_ups_used >= 0),

    created_at TEXT NOT NULL,
    completed_at TEXT,

    FOREIGN KEY(project_id)
        REFERENCES projects(id)
        ON DELETE SET NULL,

    FOREIGN KEY(task_id)
        REFERENCES tasks(id)
        ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS agent_assignments (
    id TEXT PRIMARY KEY,

    assigned_by TEXT NOT NULL,
    assigned_to TEXT NOT NULL,

    project_id TEXT,
    task_id TEXT,

    review_id TEXT,
    review_round INTEGER NOT NULL DEFAULT 1
        CHECK (review_round IN (1, 2)),

    objective TEXT NOT NULL,

    context_json TEXT,

    priority INTEGER NOT NULL DEFAULT 5
        CHECK (priority BETWEEN 1 AND 10),

    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (
            status IN (
                'queued',
                'running',
                'completed',
                'failed',
                'cancelled'
            )
        ),

    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,

    result_json TEXT,

    error_message TEXT,

    FOREIGN KEY(assigned_to)
        REFERENCES agents(id),

    FOREIGN KEY(project_id)
        REFERENCES projects(id)
        ON DELETE SET NULL,

    FOREIGN KEY(task_id)
        REFERENCES tasks(id)
        ON DELETE SET NULL,

    FOREIGN KEY(review_id)
        REFERENCES management_reviews(id)
        ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,

    assignment_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,

    started_at TEXT NOT NULL,
    completed_at TEXT,

    status TEXT NOT NULL DEFAULT 'running'
        CHECK (
            status IN (
                'running',
                'succeeded',
                'failed',
                'timed_out',
                'invalid_output'
            )
        ),

    model TEXT,

    attempts INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,

    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    -- Only stored when the provider reports it.
    cost_usd REAL,

    result_summary TEXT,
    error_message TEXT,

    FOREIGN KEY(assignment_id)
        REFERENCES agent_assignments(id)
        ON DELETE CASCADE,

    FOREIGN KEY(agent_id)
        REFERENCES agents(id)
);

CREATE INDEX IF NOT EXISTS idx_agent_assignments_agent_status
    ON agent_assignments(assigned_to, status, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_assignments_status
    ON agent_assignments(status);
CREATE INDEX IF NOT EXISTS idx_agent_assignments_review
    ON agent_assignments(review_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_assignment
    ON agent_runs(assignment_id);
CREATE INDEX IF NOT EXISTS idx_management_reviews_created
    ON management_reviews(created_at);
