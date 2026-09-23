-- Who spent it.
--
-- The ledger recorded what each call cost but not whose work it was: a
-- specialist's call carried the assignment id, and the agent's name only in
-- the free-text detail. That is enough to read a row and not enough to add
-- them up, so Catherine could report what the company spends but not what
-- each employee costs.
--
-- agent_id is nullable because not every call belongs to an employee, and a
-- plain ADD COLUMN is enough: nothing about the existing CHECK constraints
-- changes.

ALTER TABLE model_usage ADD COLUMN agent_id TEXT;

-- What can be recovered of the past: specialist and web-search rows wrote
-- the agent's id into detail, on its own or followed by " web search".
UPDATE model_usage
SET agent_id = CASE
        WHEN detail LIKE '% web search' THEN substr(detail, 1, length(detail) - 11)
        ELSE detail
    END
WHERE agent_id IS NULL
  AND source IN ('specialist', 'web_search')
  AND detail IS NOT NULL
  AND detail <> '';

CREATE INDEX IF NOT EXISTS idx_model_usage_agent ON model_usage(agent_id, occurred_at);
