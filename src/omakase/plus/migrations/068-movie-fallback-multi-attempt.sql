-- Allow up to 3 distinct fallback transfer attempts per movie request,
-- matching the multi-attempt pattern used by tv_fallback_transfers.
CREATE TABLE movie_fallback_transfers_next (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    imdb_id TEXT NOT NULL,
    request_created_at TEXT NOT NULL,
    account_binding TEXT NOT NULL CHECK(length(account_binding)=64),
    torrent_hash TEXT NOT NULL CHECK(length(torrent_hash)=40),
    torrent_id TEXT,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE(account_binding, torrent_hash),
    FOREIGN KEY(user_id, imdb_id) REFERENCES movie_requests(user_id, imdb_id) ON DELETE CASCADE
);

INSERT INTO movie_fallback_transfers_next (
    user_id,
    imdb_id,
    request_created_at,
    account_binding,
    torrent_hash,
    torrent_id,
    state
)
SELECT
    user_id,
    imdb_id,
    request_created_at,
    account_binding,
    torrent_hash,
    torrent_id,
    state
FROM movie_fallback_transfers;

DROP TABLE movie_fallback_transfers;
ALTER TABLE movie_fallback_transfers_next RENAME TO movie_fallback_transfers;

CREATE INDEX movie_fallback_request
    ON movie_fallback_transfers(user_id, imdb_id, request_created_at, account_binding, id DESC);

CREATE UNIQUE INDEX movie_fallback_one_active
    ON movie_fallback_transfers(account_binding, user_id, imdb_id, request_created_at)
    WHERE state IN ('submitting', 'submitted', 'selecting', 'selected');
