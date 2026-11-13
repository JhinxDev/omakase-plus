-- Exact actor-owned playback evidence; no aggregate history is backfilled.
CREATE TABLE web_playback_watch (
 id INTEGER PRIMARY KEY,
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
 anilist_id INTEGER NOT NULL CHECK(anilist_id>0),
 season_number INTEGER NOT NULL CHECK(season_number>0),
 episode_number INTEGER NOT NULL CHECK(episode_number>0),
 media_version TEXT NOT NULL CHECK(length(media_version)=64),
 covered_seconds REAL NOT NULL CHECK(covered_seconds>=0),
 duration_seconds REAL NOT NULL CHECK(duration_seconds>0),
 revision INTEGER NOT NULL CHECK(revision>=0),
 last_played_at TEXT NOT NULL,
 completed_at TEXT,
 completion_after_event_id INTEGER NOT NULL DEFAULT 0,
 UNIQUE(user_id,anilist_id,season_number,episode_number,media_version)
);
CREATE INDEX web_playback_watch_history ON web_playback_watch(user_id,completed_at);
CREATE TABLE deferred_completion_cleanup (
 monitor_id INTEGER PRIMARY KEY REFERENCES media_monitors(id) ON DELETE CASCADE,
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 requested_at TEXT NOT NULL
);
