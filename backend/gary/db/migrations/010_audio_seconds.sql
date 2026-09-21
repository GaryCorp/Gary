-- Audio billed by the minute, not by the token.
--
-- The transcription model charges for how long the audio is, so counting
-- tokens alone would report every spoken conversation as free. audio_seconds
-- records the duration and the price table prices it per minute; a call can
-- carry both, since a model may bill tokens and audio at the same time.
--
-- Adding the column is simple, but 'voice_transcription' has to join the
-- source CHECK, and SQLite cannot alter a CHECK constraint -- so the table is
-- rebuilt, the way 005_management_cycles.sql rebuilds for the same reason.

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
                'voice',
                -- Turning what Alex said into words, priced per minute.
                'voice_transcription',
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

    -- How much audio the call processed. Zero for a text-only call.
    audio_seconds REAL NOT NULL DEFAULT 0,

    cost_usd REAL,
    reported_cost_usd REAL,

    entity_type TEXT,
    entity_id TEXT,
    detail TEXT,

    created_at TEXT NOT NULL
);

INSERT INTO model_usage_new (
    id, occurred_at, source, model,
    input_tokens, cached_input_tokens, output_tokens,
    audio_input_tokens, audio_output_tokens, total_tokens,
    cost_usd, reported_cost_usd, entity_type, entity_id, detail, created_at
)
SELECT
    id, occurred_at, source, model,
    input_tokens, cached_input_tokens, output_tokens,
    audio_input_tokens, audio_output_tokens, total_tokens,
    cost_usd, reported_cost_usd, entity_type, entity_id, detail, created_at
FROM model_usage;

DROP TABLE model_usage;
ALTER TABLE model_usage_new RENAME TO model_usage;

CREATE INDEX IF NOT EXISTS idx_model_usage_occurred ON model_usage(occurred_at);
CREATE INDEX IF NOT EXISTS idx_model_usage_source ON model_usage(source, occurred_at);
CREATE INDEX IF NOT EXISTS idx_model_usage_entity ON model_usage(entity_type, entity_id);
