-- Personal resume and ephemeral browser writers. Existing watch tables are untouched.
CREATE TABLE web_playback_resumes (
 target_key TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
 domain TEXT NOT NULL CHECK(domain='anime'), canonical_id TEXT NOT NULL,
 season_number INTEGER NOT NULL CHECK(season_number>0), episode_number INTEGER NOT NULL CHECK(episode_number>0),
 media_version TEXT NOT NULL CHECK(length(media_version)=64), duration_seconds REAL NOT NULL CHECK(duration_seconds BETWEEN 1 AND 43200),
 position_seconds REAL NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0,
 intervals_json TEXT NOT NULL DEFAULT '[]', audio_id TEXT, text_id TEXT, updated_at REAL NOT NULL
);
CREATE TABLE web_playback_sessions (
 session_id TEXT PRIMARY KEY, target_key TEXT NOT NULL REFERENCES web_playback_resumes(target_key) ON DELETE CASCADE,
 user_id INTEGER NOT NULL, binding_digest TEXT NOT NULL, snapshot_json TEXT NOT NULL,
 revision INTEGER NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
 last_seen REAL NOT NULL, last_seq INTEGER NOT NULL DEFAULT 0, closed INTEGER NOT NULL DEFAULT 0, closed_at REAL,
 anchor_position REAL, anchor_time REAL, anchor_monotonic REAL, clock_id TEXT,
 anchor_playing INTEGER NOT NULL DEFAULT 0, anchor_rate REAL NOT NULL DEFAULT 1
);
CREATE INDEX web_playback_sessions_viewer ON web_playback_sessions(user_id,closed,last_seen);
CREATE TABLE web_playback_events (
 session_id TEXT NOT NULL REFERENCES web_playback_sessions(session_id) ON DELETE CASCADE, seq INTEGER NOT NULL,
 fingerprint TEXT NOT NULL, result_json TEXT NOT NULL, PRIMARY KEY(session_id,seq)
);

CREATE INDEX web_playback_session_expiry ON web_playback_sessions(expires_at);
CREATE INDEX web_playback_session_closed ON web_playback_sessions(closed,closed_at);
CREATE INDEX web_playback_session_inactivity ON web_playback_sessions(last_seen);
