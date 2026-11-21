CREATE TABLE title_reviews (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    domain TEXT NOT NULL CHECK(domain IN ('anime','movie','tv')),
    canonical_id TEXT NOT NULL CHECK(length(canonical_id) BETWEEN 1 AND 24),
    season_number INTEGER NOT NULL DEFAULT 0 CHECK(season_number BETWEEN 0 AND 200),
    rating REAL CHECK(rating BETWEEN 1 AND 10 AND rating*2=CAST(rating*2 AS INTEGER)),
    comment TEXT NOT NULL DEFAULT '' CHECK(length(comment)<=500),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY(user_id,domain,canonical_id,season_number),
    CHECK(season_number=0 OR domain='tv'),
    CHECK(season_number>0 OR rating IS NULL)
);
