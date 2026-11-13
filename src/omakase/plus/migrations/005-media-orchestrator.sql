CREATE TABLE IF NOT EXISTS media_monitors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    anilist_planning_id INTEGER REFERENCES anilist_plannings(id) ON DELETE SET NULL,
    anilist_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    search_titles_json TEXT NOT NULL DEFAULT '[]',
    genres_json TEXT NOT NULL DEFAULT '[]',
    format TEXT NOT NULL DEFAULT 'TV',
    release_status TEXT NOT NULL DEFAULT '',
    season_number INTEGER CHECK (
        season_number IS NULL OR (typeof(season_number) = 'integer' AND season_number >= 0)
    ),
    expected_episodes INTEGER CHECK (
        expected_episodes IS NULL
        OR (typeof(expected_episodes) = 'integer' AND expected_episodes >= 0)
    ),
    aired_episodes INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(aired_episodes) = 'integer' AND aired_episodes >= 0),
    active INTEGER NOT NULL DEFAULT 1
        CHECK (typeof(active) = 'integer' AND active IN (0, 1)),
    last_checked_at TEXT,
    last_success_at TEXT,
    next_check_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(consecutive_failures) = 'integer' AND consecutive_failures >= 0),
    last_result TEXT NOT NULL DEFAULT '' CHECK (length(last_result) <= 500),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, anilist_id)
);

CREATE TABLE IF NOT EXISTS media_sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id INTEGER NOT NULL REFERENCES media_monitors(id) ON DELETE CASCADE,
    outcome TEXT NOT NULL,
    required_count INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(required_count) = 'integer' AND required_count >= 0),
    covered_count INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(covered_count) = 'integer' AND covered_count >= 0),
    accepted_count INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(accepted_count) = 'integer' AND accepted_count >= 0),
    detail TEXT NOT NULL DEFAULT '' CHECK (length(detail) <= 500),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS media_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id INTEGER NOT NULL REFERENCES media_monitors(id) ON DELETE CASCADE,
    torrent_hash TEXT NOT NULL,
    rd_torrent_id TEXT NOT NULL DEFAULT '',
    source_title TEXT NOT NULL,
    source_folder TEXT NOT NULL DEFAULT '',
    quality TEXT NOT NULL DEFAULT '',
    seeders INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(seeders) = 'integer' AND seeders >= 0),
    ownership TEXT NOT NULL DEFAULT 'external'
        CHECK (ownership IN ('monitor_owned', 'external')),
    provenance TEXT NOT NULL DEFAULT 'pre_existing'
        CHECK (provenance IN ('omakase_created', 'adopted', 'pre_existing')),
    created_by_run_id INTEGER REFERENCES media_sync_runs(id)
        ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    shared_reference_count INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(shared_reference_count) = 'integer' AND shared_reference_count >= 0),
    downloaded_once INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(downloaded_once) = 'integer' AND downloaded_once IN (0, 1)),
    removable INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(removable) = 'integer' AND removable IN (0, 1)),
    replaces_asset_id INTEGER REFERENCES media_assets(id) ON DELETE SET NULL,
    replaced_by_asset_id INTEGER REFERENCES media_assets(id) ON DELETE SET NULL,
    planned_coverage_json TEXT NOT NULL DEFAULT '[]',
    usable_coverage_json TEXT NOT NULL DEFAULT '[]',
    numbering_override TEXT,
    inventory_fingerprint TEXT NOT NULL DEFAULT '',
    inventory_verified_at TEXT,
    status TEXT NOT NULL DEFAULT 'candidate',
    progress REAL NOT NULL DEFAULT 0 CHECK (
        typeof(progress) IN ('integer', 'real') AND progress >= 0 AND progress <= 100
    ),
    stagnant_checks INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(stagnant_checks) = 'integer' AND stagnant_checks >= 0),
    last_error TEXT NOT NULL DEFAULT '' CHECK (length(last_error) <= 500),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(monitor_id, torrent_hash),
    CHECK (provenance <> 'omakase_created' OR created_by_run_id IS NOT NULL),
    CHECK (status <> 'downloaded' OR downloaded_once = 1)
);

CREATE TABLE IF NOT EXISTS media_candidate_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id INTEGER NOT NULL REFERENCES media_monitors(id) ON DELETE CASCADE,
    sync_run_id INTEGER NOT NULL REFERENCES media_sync_runs(id)
        ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    asset_id INTEGER REFERENCES media_assets(id) ON DELETE SET NULL,
    search_alias TEXT NOT NULL,
    torrent_hash TEXT NOT NULL DEFAULT '',
    candidate_rank INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(candidate_rank) = 'integer' AND candidate_rank >= 0),
    quality TEXT NOT NULL DEFAULT '',
    seeders INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(seeders) = 'integer' AND seeders >= 0),
    proposed_coverage_json TEXT NOT NULL DEFAULT '[]',
    decision TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '' CHECK (length(reason) <= 500),
    rd_outcome TEXT NOT NULL DEFAULT '' CHECK (length(rd_outcome) <= 500),
    instant_checked INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(instant_checked) = 'integer' AND instant_checked IN (0, 1)),
    instant_available INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(instant_available) = 'integer' AND instant_available IN (0, 1)),
    terminal_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (id > 0),
    UNIQUE(monitor_id, sync_run_id, search_alias, torrent_hash, decision)
);

CREATE TABLE IF NOT EXISTS media_alias_search_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id INTEGER NOT NULL REFERENCES media_monitors(id) ON DELETE CASCADE,
    sync_run_id INTEGER NOT NULL REFERENCES media_sync_runs(id)
        ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    query_version TEXT NOT NULL,
    search_alias TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    candidate_count INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(candidate_count) = 'integer' AND candidate_count >= 0),
    eligible_1080p_count INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(eligible_1080p_count) = 'integer' AND eligible_1080p_count >= 0),
    attempted_1080p_count INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(attempted_1080p_count) = 'integer' AND attempted_1080p_count >= 0),
    cursor TEXT NOT NULL DEFAULT '',
    provider_complete INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(provider_complete) = 'integer' AND provider_complete IN (0, 1)),
    exhaustion_outcome TEXT NOT NULL DEFAULT '' CHECK (length(exhaustion_outcome) <= 500),
    UNIQUE(sync_run_id, search_alias)
);

CREATE TABLE IF NOT EXISTS media_asset_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
    sync_run_id INTEGER NOT NULL REFERENCES media_sync_runs(id)
        ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    observed_at TEXT NOT NULL,
    rd_status TEXT NOT NULL CHECK (rd_status IN ('queued', 'downloading', 'downloaded', 'error')),
    bytes_done INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(bytes_done) = 'integer' AND bytes_done >= 0),
    progress REAL NOT NULL DEFAULT 0 CHECK (
        typeof(progress) IN ('integer', 'real') AND progress >= 0 AND progress <= 100
    ),
    UNIQUE(asset_id, sync_run_id)
);

CREATE TABLE IF NOT EXISTS media_asset_removals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_key TEXT NOT NULL UNIQUE,
    asset_id INTEGER NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
    replacement_asset_id INTEGER REFERENCES media_assets(id)
        ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    candidate_attempt_id INTEGER REFERENCES media_candidate_attempts(id)
        ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    sync_run_id INTEGER NOT NULL REFERENCES media_sync_runs(id)
        ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    support_observation_oldest_id INTEGER REFERENCES media_asset_observations(id)
        ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    support_observation_newest_id INTEGER REFERENCES media_asset_observations(id)
        ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    requested_at TEXT NOT NULL,
    completed_at TEXT,
    outcome TEXT NOT NULL DEFAULT 'pending'
        CHECK (outcome IN ('pending', 'deleted', 'failed', 'refused')),
    detail TEXT NOT NULL DEFAULT '' CHECK (length(detail) <= 500),
    proof_json TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(proof_json) = 1 AND length(proof_json) <= 65536),
    CHECK (
        (outcome = 'pending' AND completed_at IS NULL)
        OR (outcome IN ('deleted', 'failed', 'refused') AND completed_at IS NOT NULL)
    ),
    CHECK (
        outcome = 'refused'
        OR (
            replacement_asset_id IS NOT NULL
            AND candidate_attempt_id IS NOT NULL
        )
    ),
    CHECK (
        (
            outcome = 'refused'
            AND support_observation_oldest_id IS NULL
            AND support_observation_newest_id IS NULL
        )
        OR (
            outcome IN ('pending', 'deleted', 'failed')
            AND support_observation_oldest_id IS NOT NULL
            AND support_observation_newest_id IS NOT NULL
            AND support_observation_oldest_id <> support_observation_newest_id
        )
    )
);

CREATE TABLE IF NOT EXISTS media_sync_lease (
    id INTEGER PRIMARY KEY CHECK (typeof(id) = 'integer' AND id = 1),
    owner TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    entrypoint TEXT NOT NULL,
    prior_owner_hash TEXT NOT NULL DEFAULT '',
    prior_expires_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS media_curator_receipt_latest (
    id INTEGER PRIMARY KEY CHECK (typeof(id) = 'integer' AND id = 1),
    receipt_json TEXT NOT NULL,
    receipt_hash TEXT NOT NULL,
    curator_run_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    received_at TEXT NOT NULL,
    completed_at TEXT
);

-- Tenant and monitor ownership validation. These triggers contain only
-- sanitized invariant names; they never expose row values in error messages.
CREATE TRIGGER IF NOT EXISTS trg_media_monitors_owner_insert
BEFORE INSERT ON media_monitors
WHEN NEW.anilist_planning_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1
        FROM anilist_plannings AS planning
        WHERE planning.id = NEW.anilist_planning_id
          AND planning.user_id = NEW.user_id
    )
BEGIN
    SELECT RAISE(ABORT, 'media monitor planning owner mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_monitors_owner_update
BEFORE UPDATE OF user_id, anilist_planning_id ON media_monitors
WHEN NEW.anilist_planning_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1
        FROM anilist_plannings AS planning
        WHERE planning.id = NEW.anilist_planning_id
          AND planning.user_id = NEW.user_id
    )
BEGIN
    SELECT RAISE(ABORT, 'media monitor planning owner mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_anilist_plannings_media_owner_update
BEFORE UPDATE OF user_id ON anilist_plannings
WHEN EXISTS (
    SELECT 1
    FROM media_monitors AS monitor
    WHERE monitor.anilist_planning_id = OLD.id
      AND monitor.user_id <> NEW.user_id
)
BEGIN
    SELECT RAISE(ABORT, 'anilist planning owner update would violate media monitor');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_assets_owner_insert
BEFORE INSERT ON media_assets
WHEN (
        NEW.created_by_run_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1
            FROM media_sync_runs AS run
            WHERE run.id = NEW.created_by_run_id
              AND run.monitor_id = NEW.monitor_id
        )
    )
    OR (
        NEW.replaces_asset_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1
            FROM media_assets AS replaced
            WHERE replaced.id = NEW.replaces_asset_id
              AND replaced.monitor_id = NEW.monitor_id
        )
    )
    OR (
        NEW.replaced_by_asset_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1
            FROM media_assets AS replacement
            WHERE replacement.id = NEW.replaced_by_asset_id
              AND replacement.monitor_id = NEW.monitor_id
        )
    )
BEGIN
    SELECT RAISE(ABORT, 'media asset monitor mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_assets_owner_update
BEFORE UPDATE OF monitor_id, created_by_run_id, replaces_asset_id, replaced_by_asset_id
ON media_assets
WHEN (
        NEW.created_by_run_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1
            FROM media_sync_runs AS run
            WHERE run.id = NEW.created_by_run_id
              AND run.monitor_id = NEW.monitor_id
        )
    )
    OR (
        NEW.replaces_asset_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1
            FROM media_assets AS replaced
            WHERE replaced.id = NEW.replaces_asset_id
              AND replaced.monitor_id = NEW.monitor_id
        )
    )
    OR (
        NEW.replaced_by_asset_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1
            FROM media_assets AS replacement
            WHERE replacement.id = NEW.replaced_by_asset_id
              AND replacement.monitor_id = NEW.monitor_id
        )
    )
BEGIN
    SELECT RAISE(ABORT, 'media asset monitor mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_assets_parent_owner_update
BEFORE UPDATE OF monitor_id ON media_assets
WHEN EXISTS (
        SELECT 1
        FROM media_assets AS linked
        WHERE (linked.replaces_asset_id = OLD.id OR linked.replaced_by_asset_id = OLD.id)
          AND linked.monitor_id <> NEW.monitor_id
    )
    OR EXISTS (
        SELECT 1
        FROM media_candidate_attempts AS attempt
        WHERE attempt.asset_id = OLD.id
          AND attempt.monitor_id <> NEW.monitor_id
    )
    OR EXISTS (
        SELECT 1
        FROM media_asset_observations AS observation
        JOIN media_sync_runs AS run ON run.id = observation.sync_run_id
        WHERE observation.asset_id = OLD.id
          AND run.monitor_id <> NEW.monitor_id
    )
    OR EXISTS (
        SELECT 1
        FROM media_asset_removals AS removal
        JOIN media_sync_runs AS run ON run.id = removal.sync_run_id
        WHERE (removal.asset_id = OLD.id OR removal.replacement_asset_id = OLD.id)
          AND run.monitor_id <> NEW.monitor_id
    )
BEGIN
    SELECT RAISE(ABORT, 'media asset monitor update would violate ownership');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_sync_runs_parent_owner_update
BEFORE UPDATE OF monitor_id ON media_sync_runs
WHEN EXISTS (
        SELECT 1
        FROM media_assets AS asset
        WHERE asset.created_by_run_id = OLD.id
          AND asset.monitor_id <> NEW.monitor_id
    )
    OR EXISTS (
        SELECT 1
        FROM media_candidate_attempts AS attempt
        WHERE attempt.sync_run_id = OLD.id
          AND attempt.monitor_id <> NEW.monitor_id
    )
    OR EXISTS (
        SELECT 1
        FROM media_alias_search_receipts AS receipt
        WHERE receipt.sync_run_id = OLD.id
          AND receipt.monitor_id <> NEW.monitor_id
    )
    OR EXISTS (
        SELECT 1
        FROM media_asset_observations AS observation
        JOIN media_assets AS asset ON asset.id = observation.asset_id
        WHERE observation.sync_run_id = OLD.id
          AND asset.monitor_id <> NEW.monitor_id
    )
    OR EXISTS (
        SELECT 1
        FROM media_asset_removals AS removal
        JOIN media_assets AS asset ON asset.id = removal.asset_id
        WHERE removal.sync_run_id = OLD.id
          AND asset.monitor_id <> NEW.monitor_id
    )
BEGIN
    SELECT RAISE(ABORT, 'media run monitor update would violate ownership');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_candidate_attempts_owner_insert
BEFORE INSERT ON media_candidate_attempts
WHEN NOT EXISTS (
        SELECT 1
        FROM media_sync_runs AS run
        WHERE run.id = NEW.sync_run_id
          AND run.monitor_id = NEW.monitor_id
    )
    OR (
        NEW.asset_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1
            FROM media_assets AS asset
            WHERE asset.id = NEW.asset_id
              AND asset.monitor_id = NEW.monitor_id
        )
    )
BEGIN
    SELECT RAISE(ABORT, 'media candidate attempt monitor mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_candidate_attempts_collision_insert
BEFORE INSERT ON media_candidate_attempts
WHEN (
        NEW.id > 0
        AND EXISTS (
            SELECT 1
            FROM media_candidate_attempts AS existing
            WHERE existing.id = NEW.id
        )
    )
    OR EXISTS (
        SELECT 1
        FROM media_candidate_attempts AS existing
        WHERE existing.monitor_id = NEW.monitor_id
          AND existing.sync_run_id = NEW.sync_run_id
          AND existing.search_alias = NEW.search_alias
          AND existing.torrent_hash = NEW.torrent_hash
          AND existing.decision = NEW.decision
    )
BEGIN
    SELECT RAISE(ABORT, 'media candidate attempt collision');
END;

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
BEGIN
    SELECT RAISE(ABORT, 'media candidate attempt immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_candidate_attempts_immutable_delete
BEFORE DELETE ON media_candidate_attempts
WHEN EXISTS (
    SELECT 1
    FROM media_monitors AS monitor
    WHERE monitor.id = OLD.monitor_id
)
BEGIN
    SELECT RAISE(ABORT, 'media candidate attempt immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_sync_runs_candidate_attempt_delete
BEFORE DELETE ON media_sync_runs
WHEN EXISTS (
        SELECT 1
        FROM media_monitors AS monitor
        WHERE monitor.id = OLD.monitor_id
    )
    AND EXISTS (
        SELECT 1
        FROM media_candidate_attempts AS attempt
        WHERE attempt.sync_run_id = OLD.id
    )
BEGIN
    SELECT RAISE(ABORT, 'media candidate attempt run delete blocked');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_candidate_attempts_owner_update
BEFORE UPDATE OF monitor_id, sync_run_id, asset_id ON media_candidate_attempts
WHEN NOT EXISTS (
        SELECT 1
        FROM media_sync_runs AS run
        WHERE run.id = NEW.sync_run_id
          AND run.monitor_id = NEW.monitor_id
    )
    OR (
        NEW.asset_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1
            FROM media_assets AS asset
            WHERE asset.id = NEW.asset_id
              AND asset.monitor_id = NEW.monitor_id
        )
    )
BEGIN
    SELECT RAISE(ABORT, 'media candidate attempt monitor mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_candidate_attempts_parent_owner_update
BEFORE UPDATE OF monitor_id ON media_candidate_attempts
WHEN EXISTS (
    SELECT 1
    FROM media_asset_removals AS removal
    JOIN media_assets AS asset ON asset.id = removal.asset_id
    WHERE removal.candidate_attempt_id = OLD.id
      AND asset.monitor_id <> NEW.monitor_id
)
BEGIN
    SELECT RAISE(ABORT, 'media candidate attempt monitor update would violate ownership');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_alias_receipts_owner_insert
BEFORE INSERT ON media_alias_search_receipts
WHEN NOT EXISTS (
    SELECT 1
    FROM media_sync_runs AS run
    WHERE run.id = NEW.sync_run_id
      AND run.monitor_id = NEW.monitor_id
)
BEGIN
    SELECT RAISE(ABORT, 'media alias receipt monitor mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_alias_receipts_owner_update
BEFORE UPDATE OF monitor_id, sync_run_id ON media_alias_search_receipts
WHEN NOT EXISTS (
    SELECT 1
    FROM media_sync_runs AS run
    WHERE run.id = NEW.sync_run_id
      AND run.monitor_id = NEW.monitor_id
)
BEGIN
    SELECT RAISE(ABORT, 'media alias receipt monitor mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_asset_observations_owner_insert
BEFORE INSERT ON media_asset_observations
WHEN NOT EXISTS (
    SELECT 1
    FROM media_assets AS asset
    JOIN media_sync_runs AS run ON run.id = NEW.sync_run_id
    WHERE asset.id = NEW.asset_id
      AND asset.monitor_id = run.monitor_id
)
BEGIN
    SELECT RAISE(ABORT, 'media asset observation monitor mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_asset_observations_owner_update
BEFORE UPDATE OF asset_id, sync_run_id ON media_asset_observations
WHEN NOT EXISTS (
    SELECT 1
    FROM media_assets AS asset
    JOIN media_sync_runs AS run ON run.id = NEW.sync_run_id
    WHERE asset.id = NEW.asset_id
      AND asset.monitor_id = run.monitor_id
)
BEGIN
    SELECT RAISE(ABORT, 'media asset observation monitor mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_asset_removals_owner_insert
BEFORE INSERT ON media_asset_removals
WHEN NOT EXISTS (
    SELECT 1
    FROM media_assets AS source
    JOIN media_sync_runs AS run
      ON run.id = NEW.sync_run_id
     AND run.monitor_id = source.monitor_id
    WHERE source.id = NEW.asset_id
      AND (
          NEW.replacement_asset_id IS NULL
          OR EXISTS (
              SELECT 1
              FROM media_assets AS replacement
              WHERE replacement.id = NEW.replacement_asset_id
                AND replacement.monitor_id = source.monitor_id
          )
      )
      AND (
          NEW.candidate_attempt_id IS NULL
          OR EXISTS (
              SELECT 1
              FROM media_candidate_attempts AS attempt
              WHERE attempt.id = NEW.candidate_attempt_id
                AND attempt.monitor_id = source.monitor_id
          )
      )
      AND (
          NEW.support_observation_oldest_id IS NULL
          OR EXISTS (
              SELECT 1
              FROM media_asset_observations AS observation
              JOIN media_assets AS support_source ON support_source.id = observation.asset_id
              WHERE observation.id = NEW.support_observation_oldest_id
                AND support_source.monitor_id = source.monitor_id
          )
      )
      AND (
          NEW.support_observation_newest_id IS NULL
          OR EXISTS (
              SELECT 1
              FROM media_asset_observations AS observation
              JOIN media_assets AS support_source ON support_source.id = observation.asset_id
              WHERE observation.id = NEW.support_observation_newest_id
                AND support_source.monitor_id = source.monitor_id
          )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'media asset removal monitor mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_asset_removals_owner_update
BEFORE UPDATE OF asset_id, replacement_asset_id, candidate_attempt_id, sync_run_id,
                 support_observation_oldest_id, support_observation_newest_id
ON media_asset_removals
WHEN NOT EXISTS (
    SELECT 1
    FROM media_assets AS source
    JOIN media_sync_runs AS run
      ON run.id = NEW.sync_run_id
     AND run.monitor_id = source.monitor_id
    WHERE source.id = NEW.asset_id
      AND (
          NEW.replacement_asset_id IS NULL
          OR EXISTS (
              SELECT 1
              FROM media_assets AS replacement
              WHERE replacement.id = NEW.replacement_asset_id
                AND replacement.monitor_id = source.monitor_id
          )
      )
      AND (
          NEW.candidate_attempt_id IS NULL
          OR EXISTS (
              SELECT 1
              FROM media_candidate_attempts AS attempt
              WHERE attempt.id = NEW.candidate_attempt_id
                AND attempt.monitor_id = source.monitor_id
          )
      )
      AND (
          NEW.support_observation_oldest_id IS NULL
          OR EXISTS (
              SELECT 1
              FROM media_asset_observations AS observation
              JOIN media_assets AS support_source ON support_source.id = observation.asset_id
              WHERE observation.id = NEW.support_observation_oldest_id
                AND support_source.monitor_id = source.monitor_id
          )
      )
      AND (
          NEW.support_observation_newest_id IS NULL
          OR EXISTS (
              SELECT 1
              FROM media_asset_observations AS observation
              JOIN media_assets AS support_source ON support_source.id = observation.asset_id
              WHERE observation.id = NEW.support_observation_newest_id
                AND support_source.monitor_id = source.monitor_id
          )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'media asset removal monitor mismatch');
END;

-- Lifecycle validation. Every error is a static invariant code: provider and
-- row values must never be copied into trigger messages.
CREATE TRIGGER IF NOT EXISTS trg_media_assets_removable_insert
BEFORE INSERT ON media_assets
WHEN NEW.removable = 1
BEGIN
    SELECT RAISE(ABORT, 'media asset removable assertion invalid');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_assets_removable_update
BEFORE UPDATE OF removable ON media_assets
WHEN (
        OLD.removable = 0
        AND NEW.removable = 1
        AND NOT EXISTS (
            SELECT 1
            FROM media_asset_removals AS removal
            WHERE removal.asset_id = OLD.id
              AND removal.outcome = 'pending'
        )
    )
    OR (
        OLD.removable = 1
        AND NEW.removable = 0
        AND EXISTS (
            SELECT 1
            FROM media_asset_removals AS removal
            WHERE removal.asset_id = OLD.id
              AND removal.outcome = 'pending'
        )
    )
BEGIN
    SELECT RAISE(ABORT, 'media asset removable assertion invalid');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_asset_observations_completed_run_insert
BEFORE INSERT ON media_asset_observations
WHEN EXISTS (
        SELECT 1
        FROM media_assets AS asset
        JOIN media_sync_runs AS run ON run.id = NEW.sync_run_id
        WHERE asset.id = NEW.asset_id
          AND asset.monitor_id = run.monitor_id
    )
    AND NOT EXISTS (
        SELECT 1
        FROM media_sync_runs AS run
        WHERE run.id = NEW.sync_run_id
          AND run.completed_at IS NOT NULL
    )
BEGIN
    SELECT RAISE(ABORT, 'media observation run incomplete');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_asset_observations_timestamp_insert
BEFORE INSERT ON media_asset_observations
WHEN EXISTS (
        SELECT 1
        FROM media_assets AS asset
        JOIN media_sync_runs AS run ON run.id = NEW.sync_run_id
        WHERE asset.id = NEW.asset_id
          AND asset.monitor_id = run.monitor_id
          AND run.completed_at IS NOT NULL
    )
    AND COALESCE(
        (
            length(NEW.observed_at) = 20
            AND strftime('%Y-%m-%dT%H:%M:%SZ', NEW.observed_at) = NEW.observed_at
            AND substr(NEW.observed_at, 12, 2) BETWEEN '00' AND '23'
            AND (
                SELECT length(run.completed_at) = 20
                   AND strftime('%Y-%m-%dT%H:%M:%SZ', run.completed_at) = run.completed_at
                   AND substr(run.completed_at, 12, 2) BETWEEN '00' AND '23'
                   AND CAST(strftime('%s', NEW.observed_at) AS INTEGER)
                       <= CAST(strftime('%s', run.completed_at) AS INTEGER)
                   AND CAST(strftime('%s', run.completed_at) AS INTEGER)
                       <= CAST(strftime('%s', 'now') AS INTEGER)
                FROM media_sync_runs AS run
                WHERE run.id = NEW.sync_run_id
            )
        ),
        0
    ) = 0
BEGIN
    SELECT RAISE(ABORT, 'media observation timestamp invalid');
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
BEGIN
    SELECT RAISE(ABORT, 'media observation immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_asset_observations_immutable_delete
BEFORE DELETE ON media_asset_observations
WHEN EXISTS (
    SELECT 1
    FROM media_assets AS asset
    WHERE asset.id = OLD.asset_id
)
BEGIN
    SELECT RAISE(ABORT, 'media observation immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_asset_observations_supersede_pending_removal
AFTER INSERT ON media_asset_observations
BEGIN
    UPDATE media_asset_removals
    SET outcome = 'failed',
        completed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
        detail = 'support observation superseded'
    WHERE asset_id = NEW.asset_id
      AND outcome = 'pending';
END;

CREATE TRIGGER IF NOT EXISTS trg_media_asset_removals_proof_insert
BEFORE INSERT ON media_asset_removals
WHEN NEW.proof_json <> '{}'
BEGIN
    SELECT RAISE(ABORT, 'media removal proof supplied');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_asset_removals_preflight_insert
BEFORE INSERT ON media_asset_removals
WHEN NEW.outcome IN ('pending', 'deleted', 'failed')
    AND NOT EXISTS (
        SELECT 1
        FROM media_assets AS source
        JOIN media_assets AS replacement ON replacement.id = NEW.replacement_asset_id
        JOIN media_candidate_attempts AS attempt ON attempt.id = NEW.candidate_attempt_id
        JOIN media_sync_runs AS attempt_run ON attempt_run.id = attempt.sync_run_id
        JOIN media_sync_runs AS decision_run ON decision_run.id = NEW.sync_run_id
        JOIN media_asset_observations AS oldest
          ON oldest.id = NEW.support_observation_oldest_id
         AND oldest.asset_id = source.id
        JOIN media_sync_runs AS oldest_run ON oldest_run.id = oldest.sync_run_id
        JOIN media_asset_observations AS newest
          ON newest.id = NEW.support_observation_newest_id
         AND newest.asset_id = source.id
        JOIN media_sync_runs AS newest_run ON newest_run.id = newest.sync_run_id
        WHERE source.id = NEW.asset_id
          AND source.ownership = 'monitor_owned'
          AND source.provenance = 'omakase_created'
          AND source.downloaded_once = 0
          AND source.shared_reference_count = 0
          AND replacement.monitor_id = source.monitor_id
          AND replacement.status = 'downloaded'
          AND replacement.downloaded_once = 1
          AND replacement.inventory_verified_at IS NOT NULL
          AND length(replacement.inventory_verified_at) = 20
          AND strftime(
              '%Y-%m-%dT%H:%M:%SZ', replacement.inventory_verified_at
          ) = replacement.inventory_verified_at
          AND substr(replacement.inventory_verified_at, 12, 2) BETWEEN '00' AND '23'
          AND CAST(strftime('%s', replacement.inventory_verified_at) AS INTEGER)
              <= CAST(strftime('%s', NEW.requested_at) AS INTEGER)
          AND CAST(strftime('%s', replacement.inventory_verified_at) AS INTEGER)
              <= CAST(strftime('%s', 'now') AS INTEGER)
          AND CASE
                  WHEN json_valid(replacement.usable_coverage_json) = 1
                       AND json_type(replacement.usable_coverage_json) = 'array'
                  THEN json_array_length(replacement.usable_coverage_json) > 0
                       AND NOT EXISTS (
                           SELECT 1
                           FROM json_each(replacement.usable_coverage_json) AS coverage
                           WHERE coverage.type <> 'integer'
                              OR coverage.atom <= 0
                       )
                       AND (
                           SELECT COUNT(*) = COUNT(DISTINCT coverage.atom)
                           FROM json_each(replacement.usable_coverage_json) AS coverage
                       )
                  ELSE 0
              END
          AND replacement.replaces_asset_id = source.id
          AND attempt.monitor_id = source.monitor_id
          AND attempt.asset_id = replacement.id
          AND attempt.decision = 'ACCEPTED'
          AND attempt.terminal_at IS NOT NULL
          AND length(attempt.terminal_at) = 20
          AND strftime('%Y-%m-%dT%H:%M:%SZ', attempt.terminal_at) = attempt.terminal_at
          AND substr(attempt.terminal_at, 12, 2) BETWEEN '00' AND '23'
          AND attempt_run.monitor_id = source.monitor_id
          AND attempt_run.completed_at IS NOT NULL
          AND length(attempt_run.completed_at) = 20
          AND strftime(
              '%Y-%m-%dT%H:%M:%SZ', attempt_run.completed_at
          ) = attempt_run.completed_at
          AND substr(attempt_run.completed_at, 12, 2) BETWEEN '00' AND '23'
          AND CAST(strftime('%s', attempt.terminal_at) AS INTEGER)
              <= CAST(strftime('%s', attempt_run.completed_at) AS INTEGER)
          AND CAST(strftime('%s', attempt_run.completed_at) AS INTEGER)
              <= CAST(strftime('%s', NEW.requested_at) AS INTEGER)
          AND CAST(strftime('%s', attempt_run.completed_at) AS INTEGER)
              <= CAST(strftime('%s', 'now') AS INTEGER)
          AND decision_run.monitor_id = source.monitor_id
          AND decision_run.completed_at IS NOT NULL
          AND CAST(strftime('%s', decision_run.completed_at) AS INTEGER)
              >= CAST(strftime('%s', replacement.inventory_verified_at) AS INTEGER)
          AND CAST(strftime('%s', decision_run.completed_at) AS INTEGER)
              >= CAST(strftime('%s', attempt.terminal_at) AS INTEGER)
          AND CAST(strftime('%s', decision_run.completed_at) AS INTEGER)
              >= CAST(strftime('%s', attempt_run.completed_at) AS INTEGER)
          AND CAST(strftime('%s', decision_run.completed_at) AS INTEGER)
              >= CAST(strftime('%s', oldest_run.completed_at) AS INTEGER)
          AND CAST(strftime('%s', decision_run.completed_at) AS INTEGER)
              >= CAST(strftime('%s', newest_run.completed_at) AS INTEGER)
          AND oldest.id <> newest.id
          AND oldest.sync_run_id <> newest.sync_run_id
          AND oldest.id = (
              SELECT observation.id
              FROM media_asset_observations AS observation
              WHERE observation.asset_id = source.id
              ORDER BY strftime('%s', observation.observed_at) DESC, observation.id DESC
              LIMIT 1 OFFSET 1
          )
          AND newest.id = (
              SELECT observation.id
              FROM media_asset_observations AS observation
              WHERE observation.asset_id = source.id
              ORDER BY strftime('%s', observation.observed_at) DESC, observation.id DESC
              LIMIT 1
          )
          AND oldest.rd_status IN ('queued', 'downloading')
          AND newest.rd_status IN ('queued', 'downloading')
          AND oldest.bytes_done = newest.bytes_done
          AND oldest.progress = newest.progress
          AND CAST(strftime('%s', newest.observed_at) AS INTEGER)
              - CAST(strftime('%s', oldest.observed_at) AS INTEGER) >= 14400
          AND oldest_run.monitor_id = source.monitor_id
          AND newest_run.monitor_id = source.monitor_id
          AND COALESCE(
              (
                  length(oldest.observed_at) = 20
                  AND strftime('%Y-%m-%dT%H:%M:%SZ', oldest.observed_at) = oldest.observed_at
                  AND substr(oldest.observed_at, 12, 2) BETWEEN '00' AND '23'
                  AND length(oldest_run.completed_at) = 20
                  AND strftime(
                      '%Y-%m-%dT%H:%M:%SZ', oldest_run.completed_at
                  ) = oldest_run.completed_at
                  AND substr(oldest_run.completed_at, 12, 2) BETWEEN '00' AND '23'
                  AND CAST(strftime('%s', oldest.observed_at) AS INTEGER)
                      <= CAST(strftime('%s', oldest_run.completed_at) AS INTEGER)
                  AND CAST(strftime('%s', oldest_run.completed_at) AS INTEGER)
                      <= CAST(strftime('%s', 'now') AS INTEGER)
              ),
              0
          ) = 1
          AND COALESCE(
              (
                  length(newest.observed_at) = 20
                  AND strftime('%Y-%m-%dT%H:%M:%SZ', newest.observed_at) = newest.observed_at
                  AND substr(newest.observed_at, 12, 2) BETWEEN '00' AND '23'
                  AND length(newest_run.completed_at) = 20
                  AND strftime(
                      '%Y-%m-%dT%H:%M:%SZ', newest_run.completed_at
                  ) = newest_run.completed_at
                  AND substr(newest_run.completed_at, 12, 2) BETWEEN '00' AND '23'
                  AND CAST(strftime('%s', newest.observed_at) AS INTEGER)
                      <= CAST(strftime('%s', newest_run.completed_at) AS INTEGER)
                  AND CAST(strftime('%s', newest_run.completed_at) AS INTEGER)
                      <= CAST(strftime('%s', 'now') AS INTEGER)
              ),
              0
          ) = 1
    )
BEGIN
    SELECT RAISE(ABORT, 'media removal preflight invalid');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_asset_removals_timestamp_insert
BEFORE INSERT ON media_asset_removals
WHEN EXISTS (
        SELECT 1
        FROM media_assets AS source
        JOIN media_sync_runs AS decision_run
          ON decision_run.id = NEW.sync_run_id
         AND decision_run.monitor_id = source.monitor_id
        WHERE source.id = NEW.asset_id
          AND (
              NEW.replacement_asset_id IS NULL
              OR EXISTS (
                  SELECT 1
                  FROM media_assets AS replacement
                  WHERE replacement.id = NEW.replacement_asset_id
                    AND replacement.monitor_id = source.monitor_id
              )
          )
          AND (
              NEW.candidate_attempt_id IS NULL
              OR EXISTS (
                  SELECT 1
                  FROM media_candidate_attempts AS attempt
                  WHERE attempt.id = NEW.candidate_attempt_id
                    AND attempt.monitor_id = source.monitor_id
              )
          )
          AND (
              NEW.support_observation_oldest_id IS NULL
              OR EXISTS (
                  SELECT 1
                  FROM media_asset_observations AS observation
                  JOIN media_assets AS support_source
                    ON support_source.id = observation.asset_id
                  WHERE observation.id = NEW.support_observation_oldest_id
                    AND support_source.monitor_id = source.monitor_id
              )
          )
          AND (
              NEW.support_observation_newest_id IS NULL
              OR EXISTS (
                  SELECT 1
                  FROM media_asset_observations AS observation
                  JOIN media_assets AS support_source
                    ON support_source.id = observation.asset_id
                  WHERE observation.id = NEW.support_observation_newest_id
                    AND support_source.monitor_id = source.monitor_id
              )
          )
    )
    AND COALESCE(
        (
            length(NEW.requested_at) = 20
            AND strftime('%Y-%m-%dT%H:%M:%SZ', NEW.requested_at) = NEW.requested_at
            AND substr(NEW.requested_at, 12, 2) BETWEEN '00' AND '23'
            AND (
                SELECT length(decision_run.completed_at) = 20
                   AND strftime(
                       '%Y-%m-%dT%H:%M:%SZ', decision_run.completed_at
                   ) = decision_run.completed_at
                   AND substr(decision_run.completed_at, 12, 2) BETWEEN '00' AND '23'
                   AND CAST(strftime('%s', decision_run.completed_at) AS INTEGER)
                       <= CAST(strftime('%s', NEW.requested_at) AS INTEGER)
                   AND CAST(strftime('%s', NEW.requested_at) AS INTEGER)
                       <= CAST(strftime('%s', 'now') AS INTEGER)
                FROM media_sync_runs AS decision_run
                WHERE decision_run.id = NEW.sync_run_id
            )
            AND (
                (NEW.outcome = 'pending' AND NEW.completed_at IS NULL)
                OR (
                    NEW.outcome IN ('deleted', 'failed', 'refused')
                    AND NEW.completed_at IS NOT NULL
                    AND length(NEW.completed_at) = 20
                    AND strftime(
                        '%Y-%m-%dT%H:%M:%SZ', NEW.completed_at
                    ) = NEW.completed_at
                    AND substr(NEW.completed_at, 12, 2) BETWEEN '00' AND '23'
                    AND CAST(strftime('%s', NEW.requested_at) AS INTEGER)
                        <= CAST(strftime('%s', NEW.completed_at) AS INTEGER)
                    AND CAST(strftime('%s', NEW.completed_at) AS INTEGER)
                        <= CAST(strftime('%s', 'now') AS INTEGER)
                )
            )
        ),
        0
    ) = 0
BEGIN
    SELECT RAISE(ABORT, 'media removal timestamp invalid');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_asset_removals_timestamp_update
BEFORE UPDATE ON media_asset_removals
WHEN OLD.outcome = 'pending'
    AND NEW.outcome IN ('deleted', 'failed')
    AND NEW.id = OLD.id
    AND NEW.operation_key = OLD.operation_key
    AND NEW.asset_id = OLD.asset_id
    AND NEW.replacement_asset_id IS OLD.replacement_asset_id
    AND NEW.candidate_attempt_id IS OLD.candidate_attempt_id
    AND NEW.sync_run_id = OLD.sync_run_id
    AND NEW.requested_at = OLD.requested_at
    AND COALESCE(
        (
            length(NEW.requested_at) = 20
            AND strftime('%Y-%m-%dT%H:%M:%SZ', NEW.requested_at) = NEW.requested_at
            AND substr(NEW.requested_at, 12, 2) BETWEEN '00' AND '23'
            AND NEW.completed_at IS NOT NULL
            AND length(NEW.completed_at) = 20
            AND strftime('%Y-%m-%dT%H:%M:%SZ', NEW.completed_at) = NEW.completed_at
            AND substr(NEW.completed_at, 12, 2) BETWEEN '00' AND '23'
            AND CAST(strftime('%s', NEW.requested_at) AS INTEGER)
                <= CAST(strftime('%s', NEW.completed_at) AS INTEGER)
            AND CAST(strftime('%s', NEW.completed_at) AS INTEGER)
                <= CAST(strftime('%s', 'now') AS INTEGER)
            AND (
                SELECT length(decision_run.completed_at) = 20
                   AND strftime(
                       '%Y-%m-%dT%H:%M:%SZ', decision_run.completed_at
                   ) = decision_run.completed_at
                   AND substr(decision_run.completed_at, 12, 2) BETWEEN '00' AND '23'
                   AND CAST(strftime('%s', decision_run.completed_at) AS INTEGER)
                       <= CAST(strftime('%s', NEW.requested_at) AS INTEGER)
                FROM media_sync_runs AS decision_run
                WHERE decision_run.id = NEW.sync_run_id
                  AND decision_run.completed_at IS NOT NULL
            )
        ),
        0
    ) = 0
BEGIN
    SELECT RAISE(ABORT, 'media removal timestamp invalid');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_asset_removals_immutable_update
BEFORE UPDATE ON media_asset_removals
WHEN EXISTS (
        SELECT 1
        FROM media_assets AS source
        JOIN media_sync_runs AS decision_run
          ON decision_run.id = NEW.sync_run_id
         AND decision_run.monitor_id = source.monitor_id
        WHERE source.id = NEW.asset_id
          AND (
              NEW.replacement_asset_id IS NULL
              OR EXISTS (
                  SELECT 1
                  FROM media_assets AS replacement
                  WHERE replacement.id = NEW.replacement_asset_id
                    AND replacement.monitor_id = source.monitor_id
              )
          )
          AND (
              NEW.candidate_attempt_id IS NULL
              OR EXISTS (
                  SELECT 1
                  FROM media_candidate_attempts AS attempt
                  WHERE attempt.id = NEW.candidate_attempt_id
                    AND attempt.monitor_id = source.monitor_id
              )
          )
          AND (
              NEW.support_observation_oldest_id IS NULL
              OR EXISTS (
                  SELECT 1
                  FROM media_asset_observations AS observation
                  JOIN media_assets AS support_source
                    ON support_source.id = observation.asset_id
                  WHERE observation.id = NEW.support_observation_oldest_id
                    AND support_source.monitor_id = source.monitor_id
              )
          )
          AND (
              NEW.support_observation_newest_id IS NULL
              OR EXISTS (
                  SELECT 1
                  FROM media_asset_observations AS observation
                  JOIN media_assets AS support_source
                    ON support_source.id = observation.asset_id
                  WHERE observation.id = NEW.support_observation_newest_id
                    AND support_source.monitor_id = source.monitor_id
              )
          )
    )
    AND NOT (
        (
            OLD.outcome IN ('pending', 'deleted', 'failed')
            AND OLD.proof_json = '{}'
            AND NEW.proof_json <> '{}'
            AND NEW.id = OLD.id
            AND NEW.operation_key = OLD.operation_key
            AND NEW.asset_id = OLD.asset_id
            AND NEW.replacement_asset_id IS OLD.replacement_asset_id
            AND NEW.candidate_attempt_id IS OLD.candidate_attempt_id
            AND NEW.sync_run_id = OLD.sync_run_id
            AND NEW.support_observation_oldest_id = OLD.support_observation_oldest_id
            AND NEW.support_observation_newest_id = OLD.support_observation_newest_id
            AND NEW.requested_at = OLD.requested_at
            AND NEW.completed_at IS OLD.completed_at
            AND NEW.outcome = OLD.outcome
            AND NEW.detail = OLD.detail
        )
        OR (
            OLD.outcome = 'pending'
            AND NEW.outcome IN ('deleted', 'failed')
            AND OLD.completed_at IS NULL
            AND NEW.completed_at IS NOT NULL
            AND NEW.id = OLD.id
            AND NEW.operation_key = OLD.operation_key
            AND NEW.asset_id = OLD.asset_id
            AND NEW.replacement_asset_id IS OLD.replacement_asset_id
            AND NEW.candidate_attempt_id IS OLD.candidate_attempt_id
            AND NEW.sync_run_id = OLD.sync_run_id
            AND NEW.support_observation_oldest_id = OLD.support_observation_oldest_id
            AND NEW.support_observation_newest_id = OLD.support_observation_newest_id
            AND NEW.requested_at = OLD.requested_at
            AND NEW.proof_json = OLD.proof_json
        )
    )
BEGIN
    SELECT RAISE(ABORT, 'media removal immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_asset_removals_immutable_delete
BEFORE DELETE ON media_asset_removals
WHEN EXISTS (
    SELECT 1
    FROM media_assets AS source
    WHERE source.id = OLD.asset_id
)
BEGIN
    SELECT RAISE(ABORT, 'media removal immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_media_asset_removals_snapshot_insert
AFTER INSERT ON media_asset_removals
WHEN NEW.outcome IN ('pending', 'deleted', 'failed')
BEGIN
    UPDATE media_asset_removals
    SET proof_json = (
        SELECT json_object(
            'source', json_object(
                'id', source.id,
                'monitor_id', source.monitor_id,
                'ownership', source.ownership,
                'provenance', source.provenance,
                'downloaded_once', source.downloaded_once,
                'shared_reference_count', source.shared_reference_count,
                'status', source.status,
                'created_at', source.created_at,
                'updated_at', source.updated_at
            ),
            'replacement', json_object(
                'id', replacement.id,
                'monitor_id', replacement.monitor_id,
                'replaces_asset_id', replacement.replaces_asset_id,
                'status', replacement.status,
                'downloaded_once', replacement.downloaded_once,
                'inventory_verified_at', replacement.inventory_verified_at,
                'inventory_fingerprint', replacement.inventory_fingerprint,
                'usable_coverage', json(replacement.usable_coverage_json),
                'created_at', replacement.created_at,
                'updated_at', replacement.updated_at
            ),
            'attempt', json_object(
                'id', attempt.id,
                'monitor_id', attempt.monitor_id,
                'sync_run_id', attempt.sync_run_id,
                'run_completed_at', attempt_run.completed_at,
                'asset_id', attempt.asset_id,
                'decision', attempt.decision,
                'terminal_at', attempt.terminal_at,
                'created_at', attempt.created_at
            ),
            'decision_run', json_object(
                'id', decision_run.id,
                'monitor_id', decision_run.monitor_id,
                'outcome', decision_run.outcome,
                'created_at', decision_run.created_at,
                'completed_at', decision_run.completed_at
            ),
            'support', json_object(
                'oldest_id', oldest.id,
                'newest_id', newest.id,
                'oldest', json_object(
                    'id', oldest.id,
                    'sync_run_id', oldest.sync_run_id,
                    'observed_at', oldest.observed_at,
                    'rd_status', oldest.rd_status,
                    'bytes_done', oldest.bytes_done,
                    'progress', oldest.progress,
                    'run_completed_at', oldest_run.completed_at
                ),
                'newest', json_object(
                    'id', newest.id,
                    'sync_run_id', newest.sync_run_id,
                    'observed_at', newest.observed_at,
                    'rd_status', newest.rd_status,
                    'bytes_done', newest.bytes_done,
                    'progress', newest.progress,
                    'run_completed_at', newest_run.completed_at
                )
            )
        )
        FROM media_assets AS source
        JOIN media_assets AS replacement ON replacement.id = NEW.replacement_asset_id
        JOIN media_candidate_attempts AS attempt ON attempt.id = NEW.candidate_attempt_id
        JOIN media_sync_runs AS attempt_run ON attempt_run.id = attempt.sync_run_id
        JOIN media_sync_runs AS decision_run ON decision_run.id = NEW.sync_run_id
        JOIN media_asset_observations AS oldest
          ON oldest.id = NEW.support_observation_oldest_id
        JOIN media_sync_runs AS oldest_run ON oldest_run.id = oldest.sync_run_id
        JOIN media_asset_observations AS newest
          ON newest.id = NEW.support_observation_newest_id
        JOIN media_sync_runs AS newest_run ON newest_run.id = newest.sync_run_id
        WHERE source.id = NEW.asset_id
    )
    WHERE id = NEW.id
      AND proof_json = '{}';
END;

CREATE TRIGGER IF NOT EXISTS trg_media_asset_removals_pending_insert
AFTER INSERT ON media_asset_removals
WHEN NEW.outcome = 'pending'
BEGIN
    UPDATE media_assets
    SET removable = 1
    WHERE id = NEW.asset_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_media_asset_removals_terminal_update
AFTER UPDATE ON media_asset_removals
WHEN OLD.outcome = 'pending' AND NEW.outcome IN ('deleted', 'failed')
BEGIN
    UPDATE media_assets
    SET removable = 0
    WHERE id = NEW.asset_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_media_assets_pending_guard_drift
BEFORE UPDATE OF ownership, provenance, downloaded_once, shared_reference_count,
                 status, inventory_verified_at, usable_coverage_json, replaces_asset_id
ON media_assets
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM media_asset_removals AS removal
        WHERE removal.outcome = 'deleted'
          AND (
              (
                  removal.asset_id = OLD.id
                  AND (
                      NEW.ownership IS NOT OLD.ownership
                      OR NEW.provenance IS NOT OLD.provenance
                      OR NEW.downloaded_once IS NOT OLD.downloaded_once
                      OR NEW.shared_reference_count IS NOT OLD.shared_reference_count
                  )
              )
              OR (
                  removal.replacement_asset_id = OLD.id
                  AND (
                      NEW.status IS NOT OLD.status
                      OR NEW.downloaded_once IS NOT OLD.downloaded_once
                      OR NEW.inventory_verified_at IS NOT OLD.inventory_verified_at
                      OR NEW.usable_coverage_json IS NOT OLD.usable_coverage_json
                      OR NEW.replaces_asset_id IS NOT OLD.replaces_asset_id
                  )
              )
          )
    ) THEN RAISE(ABORT, 'media removal proof immutable') END;

    SELECT CASE
        WHEN OLD.downloaded_once = 1 AND NEW.downloaded_once = 0
        THEN RAISE(ABORT, 'media asset downloaded_once regression')
    END;

    UPDATE media_asset_removals
    SET outcome = 'failed',
        completed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
        detail = 'preflight guard changed'
    WHERE outcome = 'pending'
      AND (
          (
              asset_id = OLD.id
              AND (
                  NEW.ownership IS NOT OLD.ownership
                  OR NEW.provenance IS NOT OLD.provenance
                  OR NEW.downloaded_once IS NOT OLD.downloaded_once
                  OR NEW.shared_reference_count IS NOT OLD.shared_reference_count
              )
          )
          OR (
              replacement_asset_id = OLD.id
              AND (
                  NEW.status IS NOT OLD.status
                  OR NEW.downloaded_once IS NOT OLD.downloaded_once
                  OR NEW.inventory_verified_at IS NOT OLD.inventory_verified_at
                  OR NEW.usable_coverage_json IS NOT OLD.usable_coverage_json
                  OR NEW.replaces_asset_id IS NOT OLD.replaces_asset_id
              )
          )
      );
END;

CREATE TRIGGER IF NOT EXISTS trg_media_assets_removable_final_update
AFTER UPDATE ON media_assets
WHEN NEW.removable = 1
    AND NOT EXISTS (
        SELECT 1
        FROM media_asset_removals AS removal
        WHERE removal.asset_id = NEW.id
          AND removal.outcome = 'pending'
    )
BEGIN
    UPDATE media_assets
    SET removable = 0
    WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_media_sync_runs_pending_guard_drift
BEFORE UPDATE OF completed_at ON media_sync_runs
BEGIN
    SELECT CASE WHEN NEW.completed_at IS NOT OLD.completed_at AND EXISTS (
        SELECT 1
        FROM media_asset_removals AS removal
        WHERE removal.outcome IN ('deleted', 'failed')
          AND (
              removal.sync_run_id = OLD.id
              OR EXISTS (
                  SELECT 1
                  FROM media_candidate_attempts AS attempt
                  WHERE attempt.id = removal.candidate_attempt_id
                    AND attempt.sync_run_id = OLD.id
              )
              OR EXISTS (
                  SELECT 1
                  FROM media_asset_observations AS supporting
                  WHERE supporting.id IN (
                            removal.support_observation_oldest_id,
                            removal.support_observation_newest_id
                        )
                    AND supporting.sync_run_id = OLD.id
              )
          )
    ) THEN RAISE(ABORT, 'media removal proof immutable') END;

    UPDATE media_asset_removals
    SET outcome = 'failed',
        completed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
        detail = 'preflight guard changed'
    WHERE outcome = 'pending'
      AND NEW.completed_at IS NOT OLD.completed_at
      AND (
          sync_run_id = OLD.id
          OR EXISTS (
              SELECT 1
              FROM media_candidate_attempts AS attempt
              WHERE attempt.id = media_asset_removals.candidate_attempt_id
                AND attempt.sync_run_id = OLD.id
          )
          OR EXISTS (
              SELECT 1
              FROM media_asset_observations AS supporting
              WHERE supporting.id IN (
                        media_asset_removals.support_observation_oldest_id,
                        media_asset_removals.support_observation_newest_id
                    )
                AND supporting.sync_run_id = OLD.id
          )
      );
END;

CREATE TRIGGER IF NOT EXISTS trg_media_assets_evidence_delete
BEFORE DELETE ON media_assets
WHEN EXISTS (
        SELECT 1
        FROM media_monitors AS monitor
        WHERE monitor.id = OLD.monitor_id
    )
    AND (
        EXISTS (
            SELECT 1
            FROM media_asset_observations AS observation
            WHERE observation.asset_id = OLD.id
        )
        OR EXISTS (
            SELECT 1
            FROM media_asset_removals AS removal
            WHERE removal.asset_id = OLD.id
        )
    )
BEGIN
    SELECT RAISE(ABORT, 'media source evidence delete blocked');
END;

CREATE INDEX IF NOT EXISTS idx_media_monitors_due
    ON media_monitors(active, next_check_at);

CREATE INDEX IF NOT EXISTS idx_media_assets_monitor_status
    ON media_assets(monitor_id, status, torrent_hash);

CREATE INDEX IF NOT EXISTS idx_media_assets_torrent_hash
    ON media_assets(torrent_hash);

CREATE INDEX IF NOT EXISTS idx_media_alias_receipts_monitor_run
    ON media_alias_search_receipts(monitor_id, sync_run_id);

CREATE INDEX IF NOT EXISTS idx_media_asset_observations_asset_time
    ON media_asset_observations(asset_id, observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_media_asset_removals_outcome_requested
    ON media_asset_removals(outcome, requested_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_media_asset_removals_one_pending
    ON media_asset_removals(asset_id)
    WHERE outcome = 'pending';
