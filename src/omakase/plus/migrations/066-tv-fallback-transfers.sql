CREATE TABLE tv_fallback_transfers (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    title_id INTEGER NOT NULL,
    season INTEGER NOT NULL CHECK(season BETWEEN 1 AND 200),
    episode INTEGER NOT NULL CHECK(episode BETWEEN 1 AND 1000),
    account_binding TEXT NOT NULL CHECK(length(account_binding)=64),
    torrent_hash TEXT NOT NULL CHECK(length(torrent_hash)=40),
    torrent_id TEXT,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE(account_binding, torrent_hash),
    FOREIGN KEY(user_id,title_id) REFERENCES tv_provider_requests(user_id,title_id) ON DELETE CASCADE
);

CREATE INDEX tv_fallback_request ON tv_fallback_transfers(user_id,title_id,account_binding,id DESC);
CREATE UNIQUE INDEX tv_fallback_one_active ON tv_fallback_transfers(account_binding,title_id)
WHERE state IN ('submitting','submitted','selecting','selected');
