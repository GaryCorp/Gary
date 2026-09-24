-- Looking for a startup product to build.
--
-- One question — "what should we build?" — is not answered by one round of
-- research. A product search asks Susan the question again and again: each
-- round is an ordinary assignment with the previous round's ranked slate and
-- its open questions in the brief, and each returns a slate of ideas scored
-- on four axes. Python ranks them, decides whether another round would add
-- anything, and stops when it would not.
--
-- The rows here are the memory between rounds: the search, what it has
-- settled on so far, and one row per round so a restart cannot run the same
-- round twice (UNIQUE (search_id, round_number), the way
-- production_episodes.episode_number works).
--
-- The ideas themselves live in each round's assignment report. This table
-- keeps only the ranked shortlist, so what Gary reads out is one row.

CREATE TABLE IF NOT EXISTS product_searches (
    id TEXT PRIMARY KEY,

    -- What Alex asked for, and the constraints the ideas must respect.
    brief TEXT NOT NULL,
    constraints_json TEXT,

    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'stopped', 'failed')),

    rounds_completed INTEGER NOT NULL DEFAULT 0,
    max_rounds INTEGER NOT NULL,

    -- The latest ranking: the whole slate as Python scored it, the winner,
    -- and what a further round would go and find out.
    shortlist_json TEXT,
    open_questions_json TEXT,
    best_idea TEXT,
    best_score REAL,

    -- Why it ended: the round limit, convergence, Susan's own confidence,
    -- a failure, or Alex.
    stop_reason TEXT,

    started_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_product_searches_status
    ON product_searches(status, updated_at);

CREATE TABLE IF NOT EXISTS product_search_rounds (
    id TEXT PRIMARY KEY,
    search_id TEXT NOT NULL REFERENCES product_searches(id) ON DELETE CASCADE,

    round_number INTEGER NOT NULL,
    assignment_id TEXT REFERENCES agent_assignments(id),

    -- The brief this round was given, so the trail is readable afterwards.
    brief TEXT NOT NULL,

    -- 'abandoned' is a round that was claimed but could never be delegated
    -- (the team was full): not work that failed, and not work that ran.
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'failed', 'abandoned')),

    ideas_considered INTEGER NOT NULL DEFAULT 0,
    top_idea TEXT,
    top_score REAL,

    created_at TEXT NOT NULL,
    completed_at TEXT,

    UNIQUE (search_id, round_number)
);

CREATE INDEX IF NOT EXISTS idx_product_search_rounds_assignment
    ON product_search_rounds(assignment_id);

-- Which report shape an assignment must come back in, when it is not the
-- agent's usual one. NULL means the agent's own report_kind, which is what
-- every existing assignment used. The roster still decides which shapes an
-- agent may be asked for (GaryCorpAgentDefinition.also_reports).
ALTER TABLE agent_assignments ADD COLUMN report_kind TEXT;
