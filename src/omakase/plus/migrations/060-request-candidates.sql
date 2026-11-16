-- Owner review W-06: Omakase keeps its own evidence about what a request's search found.
--
-- The Riven projection carries no per-candidate data, so the request page could only say
-- "received, unconfirmed". This table stores one snapshot per request of the candidates
-- Omakase found itself and the single reason each one was or was not usable. A snapshot,
-- not a log: the current rows describe the last search.
CREATE TABLE IF NOT EXISTS request_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    title_id TEXT NOT NULL,
    release_title TEXT NOT NULL,
    indexer TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    seeders INTEGER NOT NULL DEFAULT 0,
    accepted INTEGER NOT NULL DEFAULT 0,
    reason_code TEXT NOT NULL DEFAULT '',
    observed_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS request_candidates_lookup
    ON request_candidates(user_id, kind, title_id, observed_at);
