-- Movie watch history is independent from acquisition and playback readiness.
CREATE TABLE IF NOT EXISTS movie_genres (
    user_id INTEGER NOT NULL,
    imdb_id TEXT NOT NULL,
    genre_id INTEGER NOT NULL CHECK (genre_id > 0),
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
    PRIMARY KEY (user_id, imdb_id, genre_id),
    FOREIGN KEY (user_id, imdb_id) REFERENCES movie_catalog(user_id, imdb_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS movie_history (
    user_id INTEGER NOT NULL,
    imdb_id TEXT NOT NULL,
    tmdb_id INTEGER NOT NULL CHECK (tmdb_id > 0),
    status TEXT NOT NULL DEFAULT 'unwatched' CHECK (status IN ('unwatched','watching','completed','dropped')),
    rating REAL CHECK (rating IS NULL OR (rating BETWEEN 1 AND 10 AND rating * 2 = CAST(rating * 2 AS INTEGER))),
    status_source TEXT NOT NULL DEFAULT 'none' CHECK (status_source IN ('none','manual','plex')),
    rating_source TEXT NOT NULL DEFAULT 'none' CHECK (rating_source IN ('none','manual','plex')),
    status_updated_at TEXT,
    rating_updated_at TEXT,
    last_viewed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, imdb_id),
    UNIQUE (user_id, tmdb_id),
    FOREIGN KEY (user_id, imdb_id) REFERENCES movie_catalog(user_id, imdb_id) ON DELETE CASCADE
);

-- Raw snapshots stay separate so zero/absent/reset fields cannot erase user input.
CREATE TABLE IF NOT EXISTS movie_plex_history (
    user_id INTEGER NOT NULL,
    source_scope TEXT NOT NULL CHECK (length(source_scope) = 64),
    imdb_id TEXT NOT NULL,
    tmdb_id INTEGER NOT NULL CHECK (tmdb_id > 0),
    plex_key TEXT NOT NULL,
    view_count INTEGER CHECK (view_count IS NULL OR view_count >= 0),
    view_offset_ms INTEGER CHECK (view_offset_ms IS NULL OR view_offset_ms >= 0),
    last_viewed_at TEXT,
    user_rating REAL CHECK (user_rating IS NULL OR (user_rating BETWEEN 1 AND 10 AND user_rating * 2 = CAST(user_rating * 2 AS INTEGER))),
    fingerprint TEXT NOT NULL CHECK (length(fingerprint) = 64),
    observed_at TEXT NOT NULL,
    PRIMARY KEY (user_id, source_scope, imdb_id),
    FOREIGN KEY (user_id, imdb_id) REFERENCES movie_catalog(user_id, imdb_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS movie_history_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    imdb_id TEXT NOT NULL,
    field_name TEXT NOT NULL CHECK (field_name IN ('status','rating')),
    action TEXT NOT NULL CHECK (action IN ('manual','plex','follow_plex')),
    previous_json TEXT NOT NULL CHECK (json_valid(previous_json)),
    current_json TEXT NOT NULL CHECK (json_valid(current_json)),
    occurred_at TEXT NOT NULL,
    FOREIGN KEY (user_id, imdb_id) REFERENCES movie_history(user_id, imdb_id) ON DELETE CASCADE
);

-- Only whitelisted public Seerr/TMDB metadata is cached here, never mediaInfo,
-- credentials, user history, ratings, account data or personalized ordering.
CREATE TABLE IF NOT EXISTS movie_discovery_cache (
    source_scope TEXT NOT NULL CHECK (length(source_scope) = 64),
    cache_key TEXT NOT NULL CHECK (length(cache_key) = 64),
    kind TEXT NOT NULL CHECK (kind IN ('genres','candidates','detail','cooldown')),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    fetched_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    error_code TEXT,
    retry_after TEXT,
    retry_at TEXT,
    PRIMARY KEY (source_scope, cache_key)
);
CREATE INDEX IF NOT EXISTS idx_movie_history_user_status ON movie_history(user_id, status);
CREATE INDEX IF NOT EXISTS idx_movie_history_events_user ON movie_history_events(user_id, id);
CREATE INDEX IF NOT EXISTS idx_movie_discovery_cache_expiry ON movie_discovery_cache(expires_at);
