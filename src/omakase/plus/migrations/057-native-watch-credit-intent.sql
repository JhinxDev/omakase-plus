-- Anime completion credit is committed independently of the resume checkpoint.
-- The resume transaction records the intent so a crash or a tracker-side failure
-- can never leave a completion that is neither credited nor retryable. The unique
-- key is the same identity the watch event already uses, so re-recording an
-- intent for the same episode updates one row instead of creating duplicates.
CREATE TABLE IF NOT EXISTS native_watch_credits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    domain TEXT NOT NULL,
    canonical_id TEXT NOT NULL,
    season_number INTEGER NOT NULL,
    episode_number INTEGER NOT NULL,
    monitor_id INTEGER,
    event_key TEXT NOT NULL,
    playback_fraction REAL NOT NULL,
    observed_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '',
    outbox_id INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE (user_id, domain, canonical_id, season_number, episode_number)
);

CREATE INDEX IF NOT EXISTS native_watch_credits_pending
    ON native_watch_credits (status, created_at);
