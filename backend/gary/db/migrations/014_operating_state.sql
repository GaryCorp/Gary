-- Whether the company is running, and how far Alex's command emails have
-- been read.
--
-- One row, id = 1. Both facts have to survive a restart: a paused company
-- that resumed itself on the next deploy would defeat the point, and a
-- command cursor kept in memory would re-run "pause" from last week. Gary's
-- mailbox is read-only (gmail.readonly), so a command cannot be marked as
-- read on GitHub's side -- the cursor is how a message is only acted on once.
--
-- commands_cursor_ms is Gmail's internalDate (milliseconds) of the last
-- command acted on; anything older is ignored.

CREATE TABLE IF NOT EXISTS operating_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),

    paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    paused_reason TEXT,
    changed_at TEXT,
    changed_by TEXT,

    commands_cursor_ms INTEGER NOT NULL DEFAULT 0,
    last_command_id TEXT
);

INSERT OR IGNORE INTO operating_state (id, paused) VALUES (1, 0);
