CREATE TABLE IF NOT EXISTS media_title_removals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    monitor_id INTEGER NOT NULL REFERENCES media_monitors(id)
        ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    anilist_id INTEGER NOT NULL CHECK (
        typeof(anilist_id) = 'integer' AND anilist_id > 0
    ),
    season_number INTEGER CHECK (
        season_number IS NULL
        OR (typeof(season_number) = 'integer' AND season_number >= 0)
    ),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
    requested_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'awaiting_plex' CHECK (
        status IN ('awaiting_plex', 'plex_removed', 'provider_failed', 'complete')
    ),
    curator_run_id TEXT NOT NULL DEFAULT '' CHECK (length(curator_run_id) <= 80),
    plex_removed_at TEXT,
    completed_at TEXT,
    detail TEXT NOT NULL DEFAULT '' CHECK (length(detail) <= 500),
    CHECK (
        length(requested_at) = 20
        AND substr(requested_at, 5, 1) = '-'
        AND substr(requested_at, 8, 1) = '-'
        AND substr(requested_at, 11, 1) = 'T'
        AND substr(requested_at, 14, 1) = ':'
        AND substr(requested_at, 17, 1) = ':'
        AND substr(requested_at, 20, 1) = 'Z'
    ),
    CHECK (
        plex_removed_at IS NULL
        OR (
            length(plex_removed_at) = 20
            AND substr(plex_removed_at, 5, 1) = '-'
            AND substr(plex_removed_at, 8, 1) = '-'
            AND substr(plex_removed_at, 11, 1) = 'T'
            AND substr(plex_removed_at, 14, 1) = ':'
            AND substr(plex_removed_at, 17, 1) = ':'
            AND substr(plex_removed_at, 20, 1) = 'Z'
            AND plex_removed_at >= requested_at
        )
    ),
    CHECK (
        completed_at IS NULL
        OR (
            length(completed_at) = 20
            AND substr(completed_at, 5, 1) = '-'
            AND substr(completed_at, 8, 1) = '-'
            AND substr(completed_at, 11, 1) = 'T'
            AND substr(completed_at, 14, 1) = ':'
            AND substr(completed_at, 17, 1) = ':'
            AND substr(completed_at, 20, 1) = 'Z'
            AND completed_at >= requested_at
            AND (plex_removed_at IS NULL OR completed_at >= plex_removed_at)
        )
    ),
    CHECK (
        (status = 'awaiting_plex'
            AND curator_run_id = ''
            AND plex_removed_at IS NULL
            AND completed_at IS NULL)
        OR (status IN ('plex_removed', 'provider_failed')
            AND length(curator_run_id) BETWEEN 1 AND 80
            AND plex_removed_at IS NOT NULL
            AND completed_at IS NULL)
        OR (status = 'complete'
            AND length(curator_run_id) BETWEEN 1 AND 80
            AND plex_removed_at IS NOT NULL
            AND completed_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS media_title_removal_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    removal_id INTEGER NOT NULL REFERENCES media_title_removals(id) ON DELETE CASCADE,
    asset_id INTEGER NOT NULL REFERENCES media_assets(id)
        ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    provider_kind TEXT NOT NULL CHECK (
        provider_kind IN ('realdebrid', 'local_import', 'qbittorrent')
    ),
    provider_asset_id TEXT NOT NULL CHECK (length(provider_asset_id) <= 500),
    outcome TEXT NOT NULL CHECK (
        outcome IN ('pending', 'retained', 'deleted', 'absent', 'failed')
    ),
    detail TEXT NOT NULL DEFAULT '' CHECK (length(detail) <= 500),
    UNIQUE(removal_id, asset_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_media_title_removals_one_open
    ON media_title_removals(monitor_id)
    WHERE status <> 'complete';

CREATE INDEX IF NOT EXISTS idx_media_title_removals_status_requested
    ON media_title_removals(status, requested_at);

CREATE INDEX IF NOT EXISTS idx_media_title_removal_assets_removal_outcome
    ON media_title_removal_assets(removal_id, outcome);

CREATE TRIGGER IF NOT EXISTS trg_media_title_removals_owner_insert
BEFORE INSERT ON media_title_removals
WHEN NOT EXISTS (
    SELECT 1
    FROM media_monitors AS monitor
    WHERE monitor.id = NEW.monitor_id
      AND monitor.user_id = NEW.user_id
      AND monitor.anilist_id = NEW.anilist_id
      AND monitor.season_number IS NEW.season_number
)
BEGIN
    SELECT RAISE(ABORT, 'media title removal owner mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_title_removals_identity_update
BEFORE UPDATE OF user_id, monitor_id, anilist_id, season_number, title, requested_at
ON media_title_removals
BEGIN
    SELECT RAISE(ABORT, 'media title removal identity immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_title_removal_assets_owner_insert
BEFORE INSERT ON media_title_removal_assets
WHEN NOT EXISTS (
    SELECT 1
    FROM media_title_removals AS removal
    JOIN media_assets AS asset ON asset.id = NEW.asset_id
    JOIN media_monitors AS monitor ON monitor.id = asset.monitor_id
    WHERE removal.id = NEW.removal_id
      AND monitor.id = removal.monitor_id
      AND monitor.user_id = removal.user_id
)
BEGIN
    SELECT RAISE(ABORT, 'media title removal asset mismatch');
END;
