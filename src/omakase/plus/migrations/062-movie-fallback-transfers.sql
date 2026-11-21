-- Separate the direct transfer from the existing Riven submission.
CREATE TABLE movie_fallback_transfers (
    user_id INTEGER NOT NULL,
    imdb_id TEXT NOT NULL,
    request_created_at TEXT NOT NULL,
    account_binding TEXT NOT NULL CHECK(length(account_binding)=64),
    torrent_hash TEXT NOT NULL CHECK(length(torrent_hash)=40),
    torrent_id TEXT,
    state TEXT NOT NULL,
    PRIMARY KEY(user_id, imdb_id, request_created_at),
    UNIQUE(account_binding, torrent_hash),
    FOREIGN KEY(user_id, imdb_id) REFERENCES movie_requests(user_id, imdb_id) ON DELETE CASCADE
);
