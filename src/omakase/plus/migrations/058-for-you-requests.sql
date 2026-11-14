-- Recorded intent for Anime and TV. Movies keep their existing per-viewer
-- request and global provider-submission records.
CREATE TABLE for_you_requests (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('anime', 'tv')),
    title_id INTEGER NOT NULL CHECK(title_id BETWEEN 1 AND 2147483647),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 300),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY(user_id, kind, title_id)
);
CREATE INDEX for_you_requests_recent ON for_you_requests(user_id, kind, created_at DESC);
