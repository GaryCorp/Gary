-- How soon a spoken message needs Alex.
--
-- 'now' is said at the next opportunity, which is what every message did
-- before this column existed. 'next_time' is held back deliberately: Gary
-- judged it worth raising but not worth interrupting for, so it is never
-- announced and is instead handed to the next voice session for him to work
-- into the conversation. If no conversation happens, it expires with
-- everything else rather than being spoken late.
--
-- Adding a column with its own CHECK needs no table rebuild; only changing an
-- existing CHECK does (see 005_management_cycles.sql).

ALTER TABLE spoken_messages
    ADD COLUMN urgency TEXT NOT NULL DEFAULT 'now'
        CHECK (urgency IN ('now', 'next_time'));

-- The delivery pass asks for pending 'now' messages on every tick.
CREATE INDEX IF NOT EXISTS idx_spoken_urgency
    ON spoken_messages(status, urgency, created_at);
