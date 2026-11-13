ALTER TABLE media_title_removals ADD COLUMN scope TEXT NOT NULL
    DEFAULT 'plex_and_provider' CHECK (scope IN ('plex_only', 'plex_and_provider'));
ALTER TABLE media_monitors ADD COLUMN keep_completed_in_plex INTEGER NOT NULL
    DEFAULT 0 CHECK (typeof(keep_completed_in_plex) = 'integer' AND keep_completed_in_plex IN (0, 1));
CREATE TABLE user_completion_cleanup_preferences (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    remove_from_plex INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(remove_from_plex) = 'integer' AND remove_from_plex IN (0, 1))
);
CREATE TABLE completion_plex_cleanup_blocks (
    monitor_id INTEGER PRIMARY KEY REFERENCES media_monitors(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    requested_at TEXT NOT NULL,
    detail TEXT NOT NULL CHECK (length(detail) <= 500)
);
CREATE TRIGGER completion_cleanup_block_owner BEFORE INSERT ON completion_plex_cleanup_blocks
WHEN NOT EXISTS (SELECT 1 FROM media_monitors WHERE id = NEW.monitor_id AND user_id = NEW.user_id)
BEGIN SELECT RAISE(ABORT, 'completion cleanup owner mismatch'); END;
