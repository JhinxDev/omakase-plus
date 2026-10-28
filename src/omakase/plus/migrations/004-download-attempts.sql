CREATE TABLE IF NOT EXISTS download_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    anilist_planning_id INTEGER NOT NULL REFERENCES anilist_plannings(id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    candidate_rank INTEGER NOT NULL,
    total_candidates INTEGER NOT NULL DEFAULT 0,
    torrent_title TEXT NOT NULL DEFAULT '',
    torrent_hash TEXT NOT NULL DEFAULT '',
    seeders INTEGER NOT NULL DEFAULT 0,
    size_display TEXT NOT NULL DEFAULT '',
    is_batch INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    http_status INTEGER,
    error_code TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    rd_torrent_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_download_attempts_planning_created
    ON download_attempts(anilist_planning_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_download_attempts_user_created
    ON download_attempts(user_id, created_at DESC, id DESC);
