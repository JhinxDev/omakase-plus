CREATE TABLE IF NOT EXISTS web_playback_remux (
 job_id TEXT PRIMARY KEY,
 media_version TEXT NOT NULL,
 actor_user_id INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('RUNNING','READY','ERROR')),
 created REAL NOT NULL,
 expires REAL NOT NULL,
 deadline REAL,
 coordinator_pid INTEGER,
 coordinator_start_ticks TEXT,
 coordinator_boot_id TEXT,
 coordinator_token TEXT,
 phase TEXT NOT NULL DEFAULT 'admitted' CHECK(phase IN ('admitted','probe','handoff','remux','validating','complete')),
 pid INTEGER,
 start_ticks TEXT,
 boot_id TEXT,
 artifact_json TEXT,
 reserved_bytes INTEGER NOT NULL DEFAULT 805306368 CHECK(reserved_bytes=805306368)
);
CREATE UNIQUE INDEX IF NOT EXISTS web_playback_remux_single_worker
 ON web_playback_remux(state) WHERE state='RUNNING';
