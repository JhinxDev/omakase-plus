-- Movie identities, requests and byte receipts are separate from anime/watch state.
CREATE TABLE IF NOT EXISTS movie_catalog (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    imdb_id TEXT NOT NULL CHECK (length(imdb_id) BETWEEN 9 AND 12 AND substr(imdb_id, 1, 2) = 'tt' AND substr(imdb_id, 3) NOT GLOB '*[^0-9]*'),
    tmdb_id INTEGER CHECK (tmdb_id > 0),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
    year INTEGER CHECK (year BETWEEN 1870 AND 2200),
    poster_url TEXT,
    backdrop_url TEXT,
    overview TEXT,
    identity_verified INTEGER NOT NULL DEFAULT 0 CHECK (identity_verified IN (0, 1)),
    metadata_checked_at TEXT,
    metadata_error_code TEXT,
    metadata_retry_after TEXT,
    metadata_retry_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, imdb_id),
    UNIQUE (user_id, tmdb_id)
);

CREATE TABLE IF NOT EXISTS movie_requests (
    user_id INTEGER NOT NULL,
    imdb_id TEXT NOT NULL,
    requested_edition TEXT CHECK (requested_edition IS NULL OR length(requested_edition) BETWEEN 1 AND 80),
    backend_id TEXT,
    state TEXT NOT NULL DEFAULT 'requested' CHECK (state IN (
        'requested', 'searching', 'acquiring', 'verifying', 'deferred', 'unavailable'
    )),
    code TEXT NOT NULL DEFAULT 'requested',
    detail TEXT NOT NULL,
    retry_after TEXT,
    retry_at TEXT,
    investigations INTEGER NOT NULL DEFAULT 0 CHECK (investigations >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, imdb_id),
    FOREIGN KEY (user_id, imdb_id) REFERENCES movie_catalog(user_id, imdb_id) ON DELETE CASCADE
);

-- A global, credential-free reservation protects a shared Riven writer from
-- duplicate submissions, including uncertain outcomes and concurrent users.
-- Reservation is committed before HTTP. No automatic operation deletes it.
CREATE TABLE IF NOT EXISTS movie_submissions (
    backend_scope TEXT NOT NULL CHECK (length(backend_scope) = 64),
    imdb_id TEXT NOT NULL,
    tmdb_id INTEGER NOT NULL CHECK (tmdb_id > 0),
    attempted_at TEXT NOT NULL,
    backend_id TEXT,
    state TEXT NOT NULL DEFAULT 'deferred',
    code TEXT NOT NULL DEFAULT 'submission_pending',
    detail TEXT NOT NULL DEFAULT 'A submission is already being checked; no duplicate was sent.',
    retry_after TEXT,
    retry_at TEXT,
    PRIMARY KEY (backend_scope, imdb_id)
);

CREATE TABLE IF NOT EXISTS movie_verifications (
    user_id INTEGER NOT NULL,
    imdb_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL CHECK (length(fingerprint) = 64),
    plex_key TEXT NOT NULL,
    resolution TEXT,
    selected_audio TEXT,
    edition TEXT,
    state TEXT NOT NULL CHECK (state IN ('ready', 'verifying', 'deferred', 'unavailable')),
    code TEXT NOT NULL,
    detail TEXT NOT NULL,
    http_status INTEGER,
    bytes_read INTEGER NOT NULL DEFAULT 0 CHECK (bytes_read >= 0),
    verified_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    retry_after TEXT,
    retry_at TEXT,
    CHECK (state != 'ready' OR (http_status IS 206 AND bytes_read = 65536 AND code = 'ready' AND selected_audio = 'English')),
    PRIMARY KEY (user_id, imdb_id, fingerprint),
    FOREIGN KEY (user_id, imdb_id) REFERENCES movie_catalog(user_id, imdb_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_movie_requests_user_state ON movie_requests(user_id, state);
CREATE INDEX IF NOT EXISTS idx_movie_verifications_user_state ON movie_verifications(user_id, state);
