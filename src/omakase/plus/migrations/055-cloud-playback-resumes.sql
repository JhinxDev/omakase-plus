CREATE TABLE cloud_playback_resumes (
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 file_key TEXT NOT NULL,
 title TEXT NOT NULL DEFAULT '',
 position_seconds REAL NOT NULL DEFAULT 0 CHECK(position_seconds >= 0),
 duration_seconds REAL NOT NULL CHECK(duration_seconds BETWEEN 1 AND 43200),
 updated_at REAL NOT NULL,
 PRIMARY KEY(user_id, file_key)
);
CREATE INDEX cloud_playback_resumes_recent ON cloud_playback_resumes(user_id, updated_at);
