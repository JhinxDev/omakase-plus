CREATE TABLE IF NOT EXISTS plex_library_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version TEXT NOT NULL CHECK (schema_version = 'plex-library-snapshot-v1'),
    generated_at TEXT NOT NULL CHECK (length(generated_at) = 20),
    received_at TEXT NOT NULL CHECK (length(received_at) = 20),
    payload_hash TEXT NOT NULL UNIQUE CHECK (length(payload_hash) = 64),
    item_count INTEGER NOT NULL CHECK (item_count >= 0),
    title_count INTEGER NOT NULL CHECK (title_count >= 0),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS plex_library_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES plex_library_snapshots(id) ON DELETE CASCADE,
    rating_key TEXT NOT NULL CHECK (
        length(rating_key) BETWEEN 1 AND 32
        AND rating_key NOT GLOB '*[^0-9]*'
    ),
    section TEXT NOT NULL CHECK (length(section) BETWEEN 1 AND 80),
    media_type TEXT NOT NULL CHECK (media_type IN ('show', 'movie')),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
    season_number INTEGER NOT NULL CHECK (season_number >= 0),
    episode_total INTEGER NOT NULL CHECK (episode_total >= 1),
    watched_through INTEGER NOT NULL CHECK (
        watched_through >= 0 AND watched_through <= episode_total
    ),
    last_viewed_at TEXT CHECK (last_viewed_at IS NULL OR length(last_viewed_at) = 20),
    UNIQUE(snapshot_id, rating_key, season_number)
);

CREATE INDEX IF NOT EXISTS idx_plex_library_snapshots_latest
    ON plex_library_snapshots(generated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_plex_library_items_snapshot_title
    ON plex_library_items(snapshot_id, title COLLATE NOCASE, season_number);
