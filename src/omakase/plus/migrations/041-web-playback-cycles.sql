-- Resume is independent of manual corrections. Completion evidence is scoped
-- to the correction/reopen event seen by an explicitly created player session.
ALTER TABLE web_playback_sessions ADD COLUMN watch_barrier INTEGER NOT NULL DEFAULT 0;
CREATE TABLE web_playback_cycle_coverage (
 target_key TEXT NOT NULL REFERENCES web_playback_resumes(target_key) ON DELETE CASCADE,
 barrier INTEGER NOT NULL,
 intervals_json TEXT NOT NULL DEFAULT '[]',
 PRIMARY KEY(target_key,barrier)
);
CREATE TABLE web_playback_completions (
 id INTEGER PRIMARY KEY,
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 anilist_id INTEGER NOT NULL,
 season_number INTEGER NOT NULL,
 episode_number INTEGER NOT NULL,
 media_version TEXT NOT NULL,
 barrier INTEGER NOT NULL,
 completed_at TEXT NOT NULL,
 UNIQUE(user_id,anilist_id,season_number,episode_number,barrier)
);
CREATE INDEX web_playback_completions_actor ON web_playback_completions(user_id,anilist_id,season_number,barrier);
