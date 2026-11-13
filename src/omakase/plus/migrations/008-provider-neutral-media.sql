CREATE TABLE IF NOT EXISTS media_providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (
        kind IN ('realdebrid', 'local_import', 'qbittorrent')
    ),
    label TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 100),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    config_json TEXT NOT NULL DEFAULT '{}' CHECK (
        json_valid(config_json) = 1
        AND json_type(config_json) = 'object'
        AND length(config_json) <= 32768
    ),
    capabilities_json TEXT NOT NULL DEFAULT '{}' CHECK (
        json_valid(capabilities_json) = 1
        AND json_type(capabilities_json) = 'object'
        AND length(capabilities_json) <= 8192
    ),
    health_status TEXT NOT NULL DEFAULT 'unknown' CHECK (
        health_status IN ('unknown', 'healthy', 'degraded', 'error')
    ),
    health_reason_code TEXT NOT NULL DEFAULT '' CHECK (
        length(health_reason_code) <= 100
    ),
    health_detail TEXT NOT NULL DEFAULT '' CHECK (length(health_detail) <= 500),
    last_health_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, kind, label)
);

ALTER TABLE media_assets
ADD COLUMN provider_id INTEGER REFERENCES media_providers(id) ON DELETE SET NULL;

ALTER TABLE media_assets
ADD COLUMN provider_asset_id TEXT NOT NULL DEFAULT ''
CHECK (length(provider_asset_id) <= 500);

ALTER TABLE media_assets
ADD COLUMN storage_backend TEXT NOT NULL DEFAULT ''
CHECK (storage_backend IN ('', 'zurg', 'local'));

ALTER TABLE media_assets
ADD COLUMN storage_locator TEXT NOT NULL DEFAULT ''
CHECK (length(storage_locator) <= 4096);

ALTER TABLE media_candidate_attempts
ADD COLUMN provider_outcome TEXT NOT NULL DEFAULT ''
CHECK (length(provider_outcome) <= 500);

ALTER TABLE media_asset_observations
ADD COLUMN provider_status TEXT NOT NULL DEFAULT ''
CHECK (
    provider_status IN ('', 'queued', 'downloading', 'downloaded', 'error')
);

INSERT OR IGNORE INTO media_providers
    (user_id, kind, label, enabled, config_json, capabilities_json)
SELECT DISTINCT monitor.user_id,
       'realdebrid',
       'Real-Debrid',
       1,
       '{}',
       json_object(
           'acquisition', json('true'),
           'adoption', json('true'),
           'instant_check', json('true'),
           'file_selection', json('true'),
           'deletion', json('true'),
           'local_storage', json('false')
       )
FROM media_assets AS asset
JOIN media_monitors AS monitor ON monitor.id = asset.monitor_id;

UPDATE media_assets
SET provider_id = (
        SELECT provider.id
        FROM media_providers AS provider
        JOIN media_monitors AS monitor ON monitor.user_id = provider.user_id
        WHERE monitor.id = media_assets.monitor_id
          AND provider.kind = 'realdebrid'
          AND provider.label = 'Real-Debrid'
    ),
    provider_asset_id = rd_torrent_id,
    storage_backend = CASE
        WHEN source_folder <> '' THEN 'zurg'
        ELSE ''
    END,
    storage_locator = source_folder
WHERE provider_id IS NULL;

DROP TRIGGER IF EXISTS trg_media_candidate_attempts_immutable_update;
DROP TRIGGER IF EXISTS trg_media_asset_observations_immutable_update;

UPDATE media_candidate_attempts
SET provider_outcome = rd_outcome
WHERE provider_outcome = '';

UPDATE media_asset_observations
SET provider_status = rd_status
WHERE provider_status = '';

CREATE TRIGGER IF NOT EXISTS trg_media_candidate_attempts_immutable_update
BEFORE UPDATE ON media_candidate_attempts
WHEN EXISTS (
        SELECT 1
        FROM media_sync_runs AS run
        WHERE run.id = NEW.sync_run_id
          AND run.monitor_id = NEW.monitor_id
    )
    AND (
        NEW.asset_id IS NULL
        OR EXISTS (
            SELECT 1
            FROM media_assets AS asset
            WHERE asset.id = NEW.asset_id
              AND asset.monitor_id = NEW.monitor_id
        )
    )
    AND NOT EXISTS (
        SELECT 1
        FROM media_asset_removals AS removal
        JOIN media_assets AS source ON source.id = removal.asset_id
        WHERE removal.candidate_attempt_id = OLD.id
          AND source.monitor_id <> NEW.monitor_id
    )
    AND NOT (
        OLD.provider_outcome = ''
        AND NEW.provider_outcome = OLD.rd_outcome
        AND NEW.id = OLD.id
        AND NEW.monitor_id = OLD.monitor_id
        AND NEW.sync_run_id = OLD.sync_run_id
        AND NEW.asset_id IS OLD.asset_id
        AND NEW.search_alias = OLD.search_alias
        AND NEW.source_title = OLD.source_title
        AND NEW.torrent_hash = OLD.torrent_hash
        AND NEW.candidate_rank = OLD.candidate_rank
        AND NEW.quality = OLD.quality
        AND NEW.seeders = OLD.seeders
        AND NEW.proposed_coverage_json = OLD.proposed_coverage_json
        AND NEW.decision = OLD.decision
        AND NEW.reason = OLD.reason
        AND NEW.rd_outcome = OLD.rd_outcome
        AND NEW.instant_checked = OLD.instant_checked
        AND NEW.instant_available = OLD.instant_available
        AND NEW.terminal_at IS OLD.terminal_at
        AND NEW.created_at = OLD.created_at
    )
BEGIN
    SELECT RAISE(ABORT, 'media candidate attempt immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_asset_observations_immutable_update
BEFORE UPDATE ON media_asset_observations
WHEN EXISTS (
    SELECT 1
    FROM media_assets AS asset
    JOIN media_sync_runs AS run ON run.id = NEW.sync_run_id
    WHERE asset.id = NEW.asset_id
      AND asset.monitor_id = run.monitor_id
)
AND NOT (
    OLD.provider_status = ''
    AND NEW.provider_status = OLD.rd_status
    AND NEW.id = OLD.id
    AND NEW.asset_id = OLD.asset_id
    AND NEW.sync_run_id = OLD.sync_run_id
    AND NEW.observed_at = OLD.observed_at
    AND NEW.rd_status = OLD.rd_status
    AND NEW.bytes_done = OLD.bytes_done
    AND NEW.progress = OLD.progress
)
BEGIN
    SELECT RAISE(ABORT, 'media observation immutable');
END;

CREATE TABLE IF NOT EXISTS media_file_probes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
    file_locator TEXT NOT NULL CHECK (length(file_locator) BETWEEN 1 AND 4096),
    inventory_fingerprint TEXT NOT NULL CHECK (
        length(inventory_fingerprint) BETWEEN 1 AND 128
    ),
    byte_range_receipt_json TEXT NOT NULL DEFAULT '{}' CHECK (
        json_valid(byte_range_receipt_json) = 1
        AND json_type(byte_range_receipt_json) = 'object'
        AND length(byte_range_receipt_json) <= 32768
    ),
    video_streams_json TEXT NOT NULL DEFAULT '[]' CHECK (
        json_valid(video_streams_json) = 1
        AND json_type(video_streams_json) = 'array'
        AND length(video_streams_json) <= 65536
    ),
    audio_streams_json TEXT NOT NULL DEFAULT '[]' CHECK (
        json_valid(audio_streams_json) = 1
        AND json_type(audio_streams_json) = 'array'
        AND length(audio_streams_json) <= 65536
    ),
    outcome TEXT NOT NULL CHECK (
        outcome IN ('usable', 'unusable', 'error')
    ),
    detail TEXT NOT NULL DEFAULT '' CHECK (length(detail) <= 500),
    verified_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(asset_id, file_locator, inventory_fingerprint)
);

CREATE TABLE IF NOT EXISTS media_subtitle_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    probe_id INTEGER NOT NULL REFERENCES media_file_probes(id) ON DELETE CASCADE,
    stream_index INTEGER NOT NULL CHECK (stream_index >= 0),
    language TEXT NOT NULL DEFAULT 'und' CHECK (length(language) BETWEEN 2 AND 35),
    codec TEXT NOT NULL DEFAULT '' CHECK (length(codec) <= 100),
    switchable INTEGER NOT NULL DEFAULT 1 CHECK (switchable IN (0, 1)),
    default_track INTEGER NOT NULL DEFAULT 0 CHECK (default_track IN (0, 1)),
    forced INTEGER NOT NULL DEFAULT 0 CHECK (forced IN (0, 1)),
    source_kind TEXT NOT NULL CHECK (source_kind IN ('embedded', 'external')),
    sync_policy TEXT NOT NULL CHECK (
        sync_policy IN ('exact_release', 'verified_external', 'unverified_external')
    ),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(probe_id, stream_index, source_kind)
);

CREATE TABLE IF NOT EXISTS plex_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 100),
    base_url TEXT NOT NULL CHECK (length(base_url) BETWEEN 1 AND 2048),
    token_secret_name TEXT NOT NULL CHECK (
        length(token_secret_name) BETWEEN 1 AND 100
    ),
    library_map_json TEXT NOT NULL DEFAULT '{}' CHECK (
        json_valid(library_map_json) = 1
        AND json_type(library_map_json) = 'object'
        AND length(library_map_json) <= 32768
    ),
    publication_policy TEXT NOT NULL DEFAULT 'plan_only' CHECK (
        publication_policy IN ('plan_only', 'managed')
    ),
    health_status TEXT NOT NULL DEFAULT 'unknown' CHECK (
        health_status IN ('unknown', 'healthy', 'degraded', 'error')
    ),
    health_reason_code TEXT NOT NULL DEFAULT '' CHECK (
        length(health_reason_code) <= 100
    ),
    health_detail TEXT NOT NULL DEFAULT '' CHECK (length(health_detail) <= 500),
    last_health_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, label)
);

CREATE TRIGGER IF NOT EXISTS trg_media_providers_owner_update
BEFORE UPDATE OF user_id ON media_providers
WHEN EXISTS (
    SELECT 1
    FROM media_assets AS asset
    JOIN media_monitors AS monitor ON monitor.id = asset.monitor_id
    WHERE asset.provider_id = OLD.id
      AND monitor.user_id <> NEW.user_id
)
BEGIN
    SELECT RAISE(ABORT, 'media provider owner update would violate asset');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_assets_provider_insert
AFTER INSERT ON media_assets
WHEN NEW.provider_id IS NULL
BEGIN
    INSERT OR IGNORE INTO media_providers
        (user_id, kind, label, enabled, config_json, capabilities_json)
    SELECT monitor.user_id, 'realdebrid', 'Real-Debrid', 1, '{}', '{}'
    FROM media_monitors AS monitor
    WHERE monitor.id = NEW.monitor_id;

    UPDATE media_assets
    SET provider_id = (
            SELECT provider.id
            FROM media_providers AS provider
            JOIN media_monitors AS monitor ON monitor.user_id = provider.user_id
            WHERE monitor.id = NEW.monitor_id
              AND provider.kind = 'realdebrid'
              AND provider.label = 'Real-Debrid'
        ),
        provider_asset_id = NEW.rd_torrent_id,
        storage_backend = CASE
            WHEN NEW.source_folder <> '' THEN 'zurg'
            ELSE NEW.storage_backend
        END,
        storage_locator = CASE
            WHEN NEW.source_folder <> '' THEN NEW.source_folder
            ELSE NEW.storage_locator
        END
    WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_media_assets_provider_owner_insert
BEFORE INSERT ON media_assets
WHEN NEW.provider_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM media_providers AS provider
      JOIN media_monitors AS monitor ON monitor.user_id = provider.user_id
      WHERE provider.id = NEW.provider_id
        AND monitor.id = NEW.monitor_id
  )
BEGIN
    SELECT RAISE(ABORT, 'media asset provider owner mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_assets_provider_owner_update
BEFORE UPDATE OF provider_id ON media_assets
WHEN NEW.provider_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM media_providers AS provider
      JOIN media_monitors AS monitor ON monitor.user_id = provider.user_id
      WHERE provider.id = NEW.provider_id
        AND monitor.id = NEW.monitor_id
  )
BEGIN
    SELECT RAISE(ABORT, 'media asset provider owner mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_candidate_attempt_provider_projection
AFTER INSERT ON media_candidate_attempts
WHEN NEW.provider_outcome = '' AND NEW.rd_outcome <> ''
BEGIN
    UPDATE media_candidate_attempts
    SET provider_outcome = NEW.rd_outcome
    WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_media_asset_observation_provider_projection
AFTER INSERT ON media_asset_observations
WHEN NEW.provider_status = ''
BEGIN
    UPDATE media_asset_observations
    SET provider_status = NEW.rd_status
    WHERE id = NEW.id;
END;

CREATE INDEX IF NOT EXISTS idx_media_providers_user_enabled
    ON media_providers(user_id, enabled, kind);

CREATE INDEX IF NOT EXISTS idx_media_assets_provider_asset
    ON media_assets(provider_id, provider_asset_id);

CREATE INDEX IF NOT EXISTS idx_media_file_probes_asset_verified
    ON media_file_probes(asset_id, verified_at DESC);

CREATE INDEX IF NOT EXISTS idx_media_subtitle_tracks_probe_language
    ON media_subtitle_tracks(probe_id, language);

CREATE INDEX IF NOT EXISTS idx_plex_targets_user
    ON plex_targets(user_id, label);
