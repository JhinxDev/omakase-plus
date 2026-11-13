CREATE TABLE IF NOT EXISTS movie_sync_cursors (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source_scope TEXT NOT NULL CHECK (length(source_scope) = 64),
    next_offset INTEGER NOT NULL DEFAULT 0 CHECK (next_offset BETWEEN 0 AND 1000),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, source_scope)
);
