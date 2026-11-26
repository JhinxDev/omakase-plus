CREATE TABLE IF NOT EXISTS viewer_discover_layout (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('anime', 'movies', 'tv')),
    rows_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, kind)
);

CREATE TABLE IF NOT EXISTS discover_refresh_settings (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    auto_ai_refresh INTEGER NOT NULL DEFAULT 0,
    cadence TEXT NOT NULL DEFAULT 'weekly' CHECK(cadence IN ('on_new_data', 'weekly', 'manual')),
    last_signal_hash TEXT NOT NULL DEFAULT '',
    last_refreshed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
