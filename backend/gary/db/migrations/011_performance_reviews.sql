-- What GaryCorp thinks of how its people are doing, in both directions.
--
-- A review is two things kept deliberately apart. scorecard_json holds the
-- facts, computed in Python from what the company already recorded, so the
-- numbers are checkable and nobody has to take them on trust. The summary,
-- strengths, concerns and recommendations are one model's reading of those
-- facts, and are stored as what they are: judgment.
--
-- Reviews of Gary are written by the people who work for him. He reads them
-- and has to answer, which is what acknowledged_at and acknowledgement hold;
-- the answer is stored beside the criticism so neither can be quietly
-- dropped later.

CREATE TABLE IF NOT EXISTS performance_reviews (
    id TEXT PRIMARY KEY,

    created_at TEXT NOT NULL,

    -- An agent_id, the principal, or the manager himself.
    subject TEXT NOT NULL,
    subject_kind TEXT NOT NULL
        CHECK (subject_kind IN ('employee', 'principal', 'manager')),

    -- 'gary' reviewing an employee or the principal, or an agent_id
    -- reviewing gary.
    reviewer TEXT NOT NULL,

    -- The window the facts were drawn from.
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,

    -- The arithmetic, exactly as it was computed.
    scorecard_json TEXT NOT NULL,

    -- The judgment, clearly separate from the arithmetic above.
    summary TEXT NOT NULL,
    strengths_json TEXT NOT NULL DEFAULT '[]',
    concerns_json TEXT NOT NULL DEFAULT '[]',
    recommendations_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '[]',

    model TEXT,
    -- Who asked for it. A review of the principal only ever happens because
    -- he asked for it himself.
    requested_by TEXT NOT NULL DEFAULT 'gary',

    -- Only used for reviews of Gary: he must answer what the team said.
    acknowledged_at TEXT,
    acknowledgement TEXT
);

CREATE INDEX IF NOT EXISTS idx_reviews_subject
    ON performance_reviews(subject, created_at);
CREATE INDEX IF NOT EXISTS idx_reviews_unacknowledged
    ON performance_reviews(subject_kind, acknowledged_at);
