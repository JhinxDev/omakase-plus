-- Keep all existing session and resume keys. Standalone titles have no season
-- or episode; zero is reserved as their storage representation.
ALTER TABLE web_playback_events RENAME TO web_playback_events_before_targets;
ALTER TABLE web_playback_sessions RENAME TO web_playback_sessions_before_targets;
ALTER TABLE web_playback_cycle_coverage RENAME TO web_playback_cycle_coverage_before_targets;
ALTER TABLE web_playback_resumes RENAME TO web_playback_resumes_before_targets;

CREATE TABLE web_playback_resumes (
 target_key TEXT PRIMARY KEY,
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
 domain TEXT NOT NULL CHECK(domain IN ('anime','anime-film','movie','tv')),
 canonical_id TEXT NOT NULL,
 season_number INTEGER NOT NULL,
 episode_number INTEGER NOT NULL,
 media_version TEXT NOT NULL CHECK(length(media_version)=64),
 duration_seconds REAL NOT NULL CHECK(duration_seconds BETWEEN 1 AND 43200),
 position_seconds REAL NOT NULL DEFAULT 0,
 revision INTEGER NOT NULL DEFAULT 0,
 intervals_json TEXT NOT NULL DEFAULT '[]',
 audio_id TEXT, text_id TEXT, updated_at REAL NOT NULL,
 publication_identity TEXT CHECK(publication_identity IS NULL OR
   (typeof(publication_identity)='text' AND length(publication_identity)=64
    AND publication_identity NOT GLOB '*[^0-9a-f]*')),
 CHECK(
   (domain='anime' AND season_number>0 AND episode_number>0) OR
   (domain='tv' AND season_number>=0 AND episode_number>0) OR
   (domain IN ('anime-film','movie') AND season_number=0 AND episode_number=0)
 )
);
INSERT INTO web_playback_resumes SELECT * FROM web_playback_resumes_before_targets;

CREATE TABLE web_playback_sessions (
 session_id TEXT PRIMARY KEY,
 target_key TEXT NOT NULL REFERENCES web_playback_resumes(target_key) ON DELETE CASCADE,
 user_id INTEGER NOT NULL, binding_digest TEXT NOT NULL, snapshot_json TEXT NOT NULL,
 revision INTEGER NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
 last_seen REAL NOT NULL, last_seq INTEGER NOT NULL DEFAULT 0, closed INTEGER NOT NULL DEFAULT 0, closed_at REAL,
 anchor_position REAL, anchor_time REAL, anchor_monotonic REAL, clock_id TEXT,
 anchor_playing INTEGER NOT NULL DEFAULT 0, anchor_rate REAL NOT NULL DEFAULT 1,
 watch_barrier INTEGER NOT NULL DEFAULT 0
);
INSERT INTO web_playback_sessions SELECT * FROM web_playback_sessions_before_targets;

CREATE TABLE web_playback_events (
 session_id TEXT NOT NULL REFERENCES web_playback_sessions(session_id) ON DELETE CASCADE,
 seq INTEGER NOT NULL, fingerprint TEXT NOT NULL, result_json TEXT NOT NULL, PRIMARY KEY(session_id,seq)
);
INSERT INTO web_playback_events SELECT * FROM web_playback_events_before_targets;

CREATE TABLE web_playback_cycle_coverage (
 target_key TEXT NOT NULL REFERENCES web_playback_resumes(target_key) ON DELETE CASCADE,
 barrier INTEGER NOT NULL, intervals_json TEXT NOT NULL DEFAULT '[]',
 PRIMARY KEY(target_key,barrier)
);
INSERT INTO web_playback_cycle_coverage SELECT * FROM web_playback_cycle_coverage_before_targets;

DROP TABLE web_playback_events_before_targets;
DROP TABLE web_playback_sessions_before_targets;
DROP TABLE web_playback_cycle_coverage_before_targets;
DROP TABLE web_playback_resumes_before_targets;

CREATE INDEX web_playback_sessions_viewer ON web_playback_sessions(user_id,closed,last_seen);
CREATE INDEX web_playback_session_expiry ON web_playback_sessions(expires_at);
CREATE INDEX web_playback_session_closed ON web_playback_sessions(closed,closed_at);
CREATE INDEX web_playback_session_inactivity ON web_playback_sessions(last_seen);
