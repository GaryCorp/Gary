-- The weekly video schedule: which episode each production project is.
--
-- An episode's project, tasks and deadlines are ordinary rows in projects and
-- tasks, so planning, readiness and the briefing treat them like any other
-- work. This table only records that episode N exists and which project it
-- is. episode_number is the key, which is what makes planning the schedule
-- idempotent: running it twice cannot create an episode twice.
--
-- Episodes are filmed in batches, so one filming task serves several
-- episodes; film_task_id is the same for every episode in a batch.

CREATE TABLE IF NOT EXISTS production_episodes (
    episode_number INTEGER PRIMARY KEY CHECK (episode_number > 0),
    project_id TEXT NOT NULL UNIQUE REFERENCES projects(id),
    film_task_id TEXT NOT NULL REFERENCES tasks(id),
    publish_task_id TEXT NOT NULL REFERENCES tasks(id),
    shoot_at TEXT NOT NULL,
    publish_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
