-- Engineering tickets: the link between a Gary task (authoritative company
-- state) and its GitHub issue and Project item (Alex's engineering workflow).
--
-- One ticket per task, enforced by UNIQUE(task_id), so retrying ticket
-- creation can never open a second GitHub issue for the same work. Only
-- non-secret GitHub identifiers are stored here; the credential lives in the
-- environment and is never written to this database.

CREATE TABLE IF NOT EXISTS engineering_tickets (
    id TEXT PRIMARY KEY,

    task_id TEXT NOT NULL UNIQUE,

    github_owner TEXT NOT NULL,
    github_repository TEXT NOT NULL,

    github_issue_number INTEGER,
    github_issue_node_id TEXT,
    github_url TEXT,

    github_project_id TEXT,
    github_project_item_id TEXT,

    assigned_to TEXT NOT NULL,
    -- Set when GitHub accepted the assignee; 0 means Gary must not claim Alex
    -- is assigned.
    assignment_confirmed INTEGER NOT NULL DEFAULT 0,

    priority TEXT NOT NULL
        CHECK (priority IN ('P0', 'P1', 'P2', 'P3')),

    status TEXT NOT NULL
        CHECK (
            status IN (
                'backlog',
                'ready',
                'in_progress',
                'review',
                'security_review',
                'done',
                'blocked'
            )
        ),

    security_review_required INTEGER NOT NULL DEFAULT 0,
    -- Set when the ticket actually passed Security Review. A closed issue is
    -- only accepted as completed work when this is set, for tickets that
    -- require review, so nothing is marked done on GitHub's word alone.
    security_reviewed_at TEXT,

    -- Whether every GitHub step of creation finished. A ticket left
    -- incomplete is retried, never reported as fully created.
    sync_state TEXT NOT NULL DEFAULT 'pending'
        CHECK (sync_state IN ('pending', 'synced', 'degraded', 'needs_reconciliation')),
    sync_error TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_synced_at TEXT,

    FOREIGN KEY(task_id)
        REFERENCES tasks(id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_engineering_tickets_status
    ON engineering_tickets(status);

CREATE UNIQUE INDEX IF NOT EXISTS idx_engineering_tickets_issue
    ON engineering_tickets(github_owner, github_repository, github_issue_number)
    WHERE github_issue_number IS NOT NULL;

-- Discovered GitHub Project field and option node ids. Non-secret; cached so
-- normal operation does not re-read the board schema, and so no field or
-- option is created twice.
CREATE TABLE IF NOT EXISTS github_project_fields (
    project_id TEXT PRIMARY KEY,
    fields_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
