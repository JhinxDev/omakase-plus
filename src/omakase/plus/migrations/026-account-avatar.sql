-- Canonical thumbnails only. No source filename, metadata, external URL or path.
CREATE TABLE IF NOT EXISTS user_avatars (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    image_bytes BLOB NOT NULL CHECK (typeof(image_bytes) = 'blob' AND length(image_bytes) BETWEEN 1 AND 262144),
    revision TEXT NOT NULL CHECK (length(revision) = 64),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
