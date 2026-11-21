CREATE TABLE tv_provider_requests (
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'tv' CHECK(kind='tv'),
    title_id INTEGER NOT NULL,
    origin TEXT NOT NULL DEFAULT 'viewer' CHECK(origin IN ('viewer','assistant')),
    submission TEXT NOT NULL CHECK(submission IN ('sending','accepted','unconfirmed','observed')),
    seasons_json TEXT NOT NULL DEFAULT '[]' CHECK(json_type(seasons_json)='array'),
    checked_at TEXT,
    attempted_at TEXT,
    changed_at TEXT,
    snapshot_json TEXT CHECK(snapshot_json IS NULL OR json_type(snapshot_json)='object'),
    PRIMARY KEY(user_id, title_id),
    FOREIGN KEY(user_id, kind, title_id)
        REFERENCES for_you_requests(user_id, kind, title_id) ON DELETE CASCADE
);
