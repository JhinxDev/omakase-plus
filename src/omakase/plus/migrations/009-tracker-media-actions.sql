CREATE TABLE IF NOT EXISTS tracker_media_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tracker TEXT NOT NULL CHECK (tracker IN ('anilist', 'myanimelist')),
    media_id INTEGER NOT NULL CHECK (media_id > 0),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
    action TEXT NOT NULL CHECK (action IN ('completed')),
    score_ten REAL CHECK (score_ten IS NULL OR score_ten BETWEEN 1 AND 10),
    sync_state TEXT NOT NULL DEFAULT 'pending' CHECK (
        sync_state IN ('pending', 'synced', 'failed', 'unsupported')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    remote_entry_id INTEGER,
    last_error TEXT NOT NULL DEFAULT '' CHECK (length(last_error) <= 240),
    last_attempt_at TEXT,
    synced_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, tracker, media_id, action)
);

CREATE INDEX IF NOT EXISTS idx_tracker_media_actions_retry
    ON tracker_media_actions(sync_state, tracker, updated_at);

CREATE INDEX IF NOT EXISTS idx_tracker_media_actions_user
    ON tracker_media_actions(user_id, tracker, media_id);
