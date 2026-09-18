-- The continuous management loop adds a sixth planning type, 'management':
-- the short reactive cycle that runs between the three scheduled ones.
--
-- SQLite cannot alter a CHECK constraint, so planning_runs is rebuilt with
-- the new value allowed and its rows copied across. Everything else about
-- the table is unchanged.

CREATE TABLE planning_runs_new (
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
                'manual',
                'management'
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

INSERT INTO planning_runs_new
    (id, started_at, completed_at, planning_type, input_summary, plan_json,
     status, error_message)
SELECT
    id, started_at, completed_at, planning_type, input_summary, plan_json,
    status, error_message
FROM planning_runs;

DROP TABLE planning_runs;

ALTER TABLE planning_runs_new RENAME TO planning_runs;

CREATE INDEX IF NOT EXISTS idx_planning_runs_started ON planning_runs(started_at);
