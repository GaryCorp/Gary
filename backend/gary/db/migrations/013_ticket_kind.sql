-- What a ticket is for. Engineering tickets are code: they go through
-- Review (and Security Review when required) and are only complete when the
-- Project says Done. Production tickets are the video schedule's stages
-- (script, film, edit, thumbnail, publish): there is nothing to review, so
-- closing the issue completes the stage and completing the stage closes the
-- issue.
--
-- ADD COLUMN, not a table rebuild: the column is new, so there is no
-- existing CHECK to change, and every existing ticket is engineering.

ALTER TABLE engineering_tickets
    ADD COLUMN kind TEXT NOT NULL DEFAULT 'engineering'
        CHECK (kind IN ('engineering', 'production'));
