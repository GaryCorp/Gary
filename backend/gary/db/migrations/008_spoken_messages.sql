-- What Gary said out loud, and what he is still waiting to hear back.
--
-- Speech does not persist: if the voice service was down, if it was quiet
-- hours, or if Alex was simply out of the room, an announcement used to be
-- gone with nothing to show it ever happened. A row is written here *before*
-- anything is spoken, so the decision to speak survives a restart, and it is
-- marked spoken only once a voice client has actually received it.
--
-- This table is the source of truth. The note Gary keeps in the Joplin
-- "Spoken" notebook is a mirror for Alex to read: joplin_note_id stays NULL
-- until that write succeeds, and the delivery pass retries it, so a Joplin
-- outage delays the note and never loses the message.

CREATE TABLE IF NOT EXISTS spoken_messages (
    id TEXT PRIMARY KEY,

    created_at TEXT NOT NULL,

    -- A question wants an answer back; a notice is Gary keeping Alex informed.
    kind TEXT NOT NULL
        CHECK (kind IN ('question', 'notice')),

    -- Spoken words only. Piper reads this aloud, so no markdown and no
    -- symbols (see Gary's prompt).
    text TEXT NOT NULL,

    expects_reply INTEGER NOT NULL DEFAULT 0
        CHECK (expects_reply IN (0, 1)),

    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (
            status IN (
                'pending',
                'spoken',
                'answered',
                'expired',
                'superseded'
            )
        ),

    -- Which part of the company decided to speak.
    source TEXT NOT NULL
        CHECK (
            source IN (
                'voice',
                'planning_cycle',
                'management_loop',
                'approval',
                'email',
                'operations',
                'briefing'
            )
        ),

    -- Normalized words, so the same question is not raised twice while one
    -- is still open.
    topic_key TEXT NOT NULL DEFAULT '',

    -- What this is about, when it is about something.
    approval_id TEXT,
    action_id TEXT,
    project_id TEXT,
    task_id TEXT,

    -- When it was first said, when it was last said, and how many times Alex
    -- has asked for it again.
    spoken_at TEXT,
    last_spoken_at TEXT,
    repeat_count INTEGER NOT NULL DEFAULT 0,

    answered_at TEXT,
    answer TEXT,

    joplin_note_id TEXT,
    joplin_written_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_spoken_status ON spoken_messages(status, created_at);
CREATE INDEX IF NOT EXISTS idx_spoken_created ON spoken_messages(created_at);
CREATE INDEX IF NOT EXISTS idx_spoken_approval ON spoken_messages(approval_id);
-- The delivery pass asks for these two on every tick.
CREATE INDEX IF NOT EXISTS idx_spoken_unmirrored
    ON spoken_messages(joplin_written_at, spoken_at);
