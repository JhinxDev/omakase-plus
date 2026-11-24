ALTER TABLE movie_catalog ADD COLUMN original_title TEXT;

CREATE TABLE tv_season_pack_transfers (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    title_id INTEGER NOT NULL,
    season INTEGER NOT NULL CHECK(season BETWEEN 1 AND 200),
    account_binding TEXT NOT NULL CHECK(length(account_binding)=64),
    torrent_hash TEXT NOT NULL CHECK(length(torrent_hash)=40),
    torrent_id TEXT CHECK(torrent_id IS NULL OR length(torrent_id) BETWEEN 3 AND 64),
    state TEXT NOT NULL CHECK(state IN (
        'submitting','submission_unknown','submitted','selecting','selected','downloaded',
        'existing_copy','file_mismatch','provider_restricted','provider_failed',
        'provider_missing','stalled','account_changed'
    )),
    selected_episodes_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX tv_season_pack_request
    ON tv_season_pack_transfers(user_id, title_id, account_binding, id DESC);
CREATE UNIQUE INDEX tv_season_pack_one_active
    ON tv_season_pack_transfers(account_binding, title_id)
    WHERE state IN ('submitting','submitted','selecting','selected');
