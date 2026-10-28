CREATE TABLE IF NOT EXISTS media_catalog_health (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    catalog_kind TEXT NOT NULL,
    label TEXT NOT NULL,
    health_status TEXT NOT NULL
        CHECK (health_status IN ('healthy', 'degraded', 'error', 'unknown')),
    reason_code TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    candidate_count INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(candidate_count) = 'integer' AND candidate_count >= 0),
    checked_at TEXT NOT NULL,
    PRIMARY KEY (user_id, catalog_kind),
    CHECK (length(catalog_kind) BETWEEN 1 AND 100),
    CHECK (length(label) BETWEEN 1 AND 200),
    CHECK (length(reason_code) <= 100),
    CHECK (length(detail) <= 500),
    CHECK (
        length(checked_at) = 20
        AND substr(checked_at, 5, 1) = '-'
        AND substr(checked_at, 8, 1) = '-'
        AND substr(checked_at, 11, 1) = 'T'
        AND substr(checked_at, 14, 1) = ':'
        AND substr(checked_at, 17, 1) = ':'
        AND substr(checked_at, 20, 1) = 'Z'
    )
);

CREATE INDEX IF NOT EXISTS idx_media_catalog_health_status
ON media_catalog_health(user_id, health_status, checked_at);
