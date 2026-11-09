CREATE TABLE movie_cleanup_preferences (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1))
);

CREATE TABLE movie_cleanup_jobs (
    user_id INTEGER NOT NULL,
    imdb_id TEXT NOT NULL,
    cleanup_tag TEXT NOT NULL,
    backend_scope TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'cancelled')),
    PRIMARY KEY (user_id, imdb_id, cleanup_tag),
    FOREIGN KEY (user_id, imdb_id) REFERENCES movie_history(user_id, imdb_id) ON DELETE CASCADE
);
