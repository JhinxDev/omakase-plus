ALTER TABLE movie_cleanup_jobs
ADD COLUMN intent TEXT NOT NULL DEFAULT 'automatic' CHECK (intent IN ('automatic', 'manual'));

ALTER TABLE movie_cleanup_jobs
ADD COLUMN selection_digest TEXT CHECK (selection_digest IS NULL OR length(selection_digest) = 64);

CREATE TABLE movie_cleanup_confirmations (
    user_id INTEGER NOT NULL,
    imdb_id TEXT NOT NULL,
    nonce_digest TEXT NOT NULL CHECK (length(nonce_digest) = 64),
    selection_digest TEXT NOT NULL CHECK (length(selection_digest) = 64),
    cleanup_tag TEXT NOT NULL,
    backend_scope TEXT NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (user_id, imdb_id),
    FOREIGN KEY (user_id, imdb_id) REFERENCES movie_history(user_id, imdb_id) ON DELETE CASCADE
);
