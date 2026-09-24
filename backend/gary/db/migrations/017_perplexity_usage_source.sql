-- Susan's deep research is a paid search of its own, at a second provider.
--
-- perplexity_search calls Perplexity, not OpenAI, on a different model at a
-- different price. Folding it into 'web_search' would report two providers as
-- one line and make the per-model prices meaningless, so it gets its own
-- source, the way 'performance_review' did in 015.
--
-- SQLite cannot alter a CHECK constraint, so the table is rebuilt.

CREATE TABLE model_usage_new (
    id TEXT PRIMARY KEY,

    occurred_at TEXT NOT NULL,

    -- Which part of the company spent it.
    source TEXT NOT NULL
        CHECK (
            source IN (
                'planning_cycle',
                'specialist',
                'web_search',
                -- Susan's deeper search, billed by Perplexity.
                'perplexity_search',
                'voice',
                -- Turning what Alex said into words, priced per minute.
                'voice_transcription',
                -- Judging how someone is doing, in a review.
                'performance_review',
                'other'
            )
        ),

    model TEXT NOT NULL,

    input_tokens INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    audio_input_tokens INTEGER NOT NULL DEFAULT 0,
    audio_output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,

    audio_seconds REAL NOT NULL DEFAULT 0,

    cost_usd REAL,
    reported_cost_usd REAL,

    entity_type TEXT,
    entity_id TEXT,
    detail TEXT,
    agent_id TEXT,

    created_at TEXT NOT NULL
);

INSERT INTO model_usage_new (
    id, occurred_at, source, model,
    input_tokens, cached_input_tokens, output_tokens,
    audio_input_tokens, audio_output_tokens, total_tokens,
    audio_seconds, cost_usd, reported_cost_usd, entity_type, entity_id, detail, agent_id, created_at
)
SELECT
    id, occurred_at, source, model,
    input_tokens, cached_input_tokens, output_tokens,
    audio_input_tokens, audio_output_tokens, total_tokens,
    audio_seconds, cost_usd, reported_cost_usd, entity_type, entity_id, detail, agent_id, created_at
FROM model_usage;

DROP TABLE model_usage;
ALTER TABLE model_usage_new RENAME TO model_usage;

CREATE INDEX IF NOT EXISTS idx_model_usage_occurred ON model_usage(occurred_at);
CREATE INDEX IF NOT EXISTS idx_model_usage_source ON model_usage(source, occurred_at);
CREATE INDEX IF NOT EXISTS idx_model_usage_entity ON model_usage(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_model_usage_agent ON model_usage(agent_id, occurred_at);
