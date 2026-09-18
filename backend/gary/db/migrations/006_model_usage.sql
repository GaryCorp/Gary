-- What GaryCorp's thinking costs.
--
-- Every model call the company makes is recorded here: Gary's planning
-- cycles, the specialists' runs, their web searches, and the voice
-- conversations. agent_runs keeps per-assignment detail; this is the ledger
-- Catherine reports from, so one row per call, priced where a price is known.
--
-- cost_usd is computed from the deployment's price table and is NULL when the
-- model has no price set: an unpriced call is reported as unpriced, never as
-- free. reported_cost_usd is only filled when the provider itself returned a
-- cost.

CREATE TABLE IF NOT EXISTS model_usage (
    id TEXT PRIMARY KEY,

    occurred_at TEXT NOT NULL,

    -- Which part of the company spent it.
    source TEXT NOT NULL
        CHECK (
            source IN (
                'planning_cycle',
                'specialist',
                'web_search',
                'voice',
                'other'
            )
        ),

    model TEXT NOT NULL,

    input_tokens INTEGER NOT NULL DEFAULT 0,
    -- Cached input is billed at a lower rate by most providers.
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    -- Realtime audio is priced separately from text.
    audio_input_tokens INTEGER NOT NULL DEFAULT 0,
    audio_output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,

    cost_usd REAL,
    reported_cost_usd REAL,

    -- What the call was for: a planning run, an assignment, a conversation.
    entity_type TEXT,
    entity_id TEXT,
    detail TEXT,

    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_model_usage_occurred ON model_usage(occurred_at);
CREATE INDEX IF NOT EXISTS idx_model_usage_source ON model_usage(source, occurred_at);
CREATE INDEX IF NOT EXISTS idx_model_usage_entity ON model_usage(entity_type, entity_id);
