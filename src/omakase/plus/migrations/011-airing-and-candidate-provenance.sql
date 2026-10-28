ALTER TABLE media_monitors
ADD COLUMN next_airing_at INTEGER
CHECK (
    next_airing_at IS NULL
    OR (typeof(next_airing_at) = 'integer' AND next_airing_at > 0)
);

ALTER TABLE media_candidate_attempts
ADD COLUMN provider_kind TEXT NOT NULL DEFAULT 'realdebrid'
CHECK (length(provider_kind) BETWEEN 1 AND 100);

ALTER TABLE media_candidate_attempts
ADD COLUMN catalog_kind TEXT NOT NULL DEFAULT 'nyaa'
CHECK (length(catalog_kind) BETWEEN 1 AND 100);

CREATE INDEX IF NOT EXISTS idx_media_candidate_attempts_provider_block
ON media_candidate_attempts(monitor_id, provider_kind, decision, created_at, torrent_hash);
