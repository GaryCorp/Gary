-- Employees GaryCorp hired for itself.
--
-- The static roster in gary/agents/roster.py stays the authority for what an
-- agent may do: this table adds *who* exists, never *what they may do beyond
-- the code's ceiling*. allowed_tools is re-validated against HIREABLE_TOOLS
-- every time the roster is loaded, so editing this table by hand cannot grant
-- a capability the shipped code does not allow.
--
-- A row only appears here after Alex approved the hire on the web page; the
-- approval and the proposing action are recorded alongside it, so the audit
-- trail runs proposal -> approval -> roster entry.

CREATE TABLE IF NOT EXISTS hired_employees (
    agent_id TEXT PRIMARY KEY
        CHECK (agent_id GLOB '[a-z][a-z0-9_]*'),

    name TEXT NOT NULL,
    title TEXT NOT NULL,
    department TEXT NOT NULL,

    -- The agent's own top-level Joplin notebook, unique across the company.
    notebook TEXT NOT NULL,

    -- What Gary wrote: the specialty and manner. The immutable part of the
    -- prompt is composed in code and is not stored here.
    specialty TEXT NOT NULL,
    personality TEXT,
    -- Why the company needed this role, kept for the record.
    capability_gap TEXT NOT NULL,

    allowed_tools TEXT NOT NULL,

    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'deactivated')),

    -- proposal -> approval -> roster entry.
    proposed_by TEXT NOT NULL DEFAULT 'gary',
    action_id TEXT,
    approval_id TEXT,
    approved_by TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deactivated_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_hired_employees_notebook
    ON hired_employees(lower(notebook))
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_hired_employees_status ON hired_employees(status);
