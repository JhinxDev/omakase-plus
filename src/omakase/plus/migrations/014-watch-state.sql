CREATE TABLE IF NOT EXISTS watch_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    monitor_id INTEGER UNIQUE REFERENCES media_monitors(id) ON DELETE SET NULL,
    anilist_id INTEGER NOT NULL CHECK (
        typeof(anilist_id) = 'integer' AND anilist_id > 0
    ),
    season_number INTEGER NOT NULL DEFAULT 0 CHECK (
        typeof(season_number) = 'integer' AND season_number >= 0
    ),
    status TEXT NOT NULL DEFAULT 'unwatched' CHECK (
        status IN ('unwatched', 'watching', 'completed')
    ),
    progress_episodes INTEGER NOT NULL DEFAULT 0 CHECK (
        typeof(progress_episodes) = 'integer' AND progress_episodes >= 0
    ),
    episode_total INTEGER CHECK (
        episode_total IS NULL
        OR (typeof(episode_total) = 'integer' AND episode_total > 0)
    ),
    last_source TEXT NOT NULL DEFAULT 'manual' CHECK (
        last_source IN ('plex', 'apple_tv', 'anilist', 'manual', 'migration')
    ),
    confidence TEXT NOT NULL DEFAULT 'manual' CHECK (
        confidence IN ('exact', 'inferred', 'manual')
    ),
    last_observed_at TEXT CHECK (
        last_observed_at IS NULL OR length(last_observed_at) BETWEEN 20 AND 40
    ),
    completed_at TEXT CHECK (
        completed_at IS NULL OR length(completed_at) BETWEEN 19 AND 40
    ),
    revision INTEGER NOT NULL DEFAULT 0 CHECK (
        typeof(revision) = 'integer' AND revision >= 0
    ),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, anilist_id, season_number),
    CHECK (episode_total IS NULL OR progress_episodes <= episode_total),
    CHECK (
        (status = 'completed' AND completed_at IS NOT NULL)
        OR (status <> 'completed' AND completed_at IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS watch_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_entry_id INTEGER NOT NULL REFERENCES watch_entries(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (
        source IN ('plex', 'apple_tv', 'anilist', 'manual', 'migration')
    ),
    event_key TEXT NOT NULL CHECK (length(event_key) BETWEEN 1 AND 240),
    kind TEXT NOT NULL CHECK (
        kind IN ('progress', 'completed', 'correction', 'reopened')
    ),
    episode INTEGER CHECK (
        episode IS NULL OR (typeof(episode) = 'integer' AND episode >= 0)
    ),
    playback_fraction REAL CHECK (
        playback_fraction IS NULL OR playback_fraction BETWEEN 0.0 AND 1.0
    ),
    confidence TEXT NOT NULL DEFAULT 'exact' CHECK (
        confidence IN ('exact', 'inferred', 'manual')
    ),
    force INTEGER NOT NULL DEFAULT 0 CHECK (
        typeof(force) = 'integer' AND force IN (0, 1)
    ),
    disposition TEXT NOT NULL CHECK (
        disposition IN ('accepted', 'ignored', 'confirmation_required')
    ),
    reason TEXT NOT NULL DEFAULT '' CHECK (length(reason) <= 240),
    observed_at TEXT NOT NULL CHECK (length(observed_at) BETWEEN 20 AND 40),
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK (length(payload_json) <= 4096),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, source, event_key)
);

CREATE TABLE IF NOT EXISTS watch_confirmations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_entry_id INTEGER NOT NULL REFERENCES watch_entries(id) ON DELETE CASCADE,
    watch_event_id INTEGER NOT NULL UNIQUE REFERENCES watch_events(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'accepted', 'rejected', 'expired')
    ),
    proposed_episode INTEGER CHECK (
        proposed_episode IS NULL
        OR (typeof(proposed_episode) = 'integer' AND proposed_episode > 0)
    ),
    prompt TEXT NOT NULL CHECK (length(prompt) BETWEEN 1 AND 500),
    resolved_at TEXT CHECK (
        resolved_at IS NULL OR length(resolved_at) BETWEEN 19 AND 40
    ),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (
        (status = 'pending' AND resolved_at IS NULL)
        OR (status <> 'pending' AND resolved_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS tracker_sync_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_entry_id INTEGER NOT NULL REFERENCES watch_entries(id) ON DELETE CASCADE,
    watch_event_id INTEGER NOT NULL UNIQUE REFERENCES watch_events(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tracker TEXT NOT NULL DEFAULT 'anilist' CHECK (tracker IN ('anilist')),
    action TEXT NOT NULL CHECK (action IN ('progress', 'complete')),
    progress_episodes INTEGER NOT NULL CHECK (
        typeof(progress_episodes) = 'integer' AND progress_episodes >= 0
    ),
    sync_state TEXT NOT NULL DEFAULT 'pending' CHECK (
        sync_state IN ('pending', 'leased', 'synced', 'failed', 'conflict')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (
        typeof(attempt_count) = 'integer' AND attempt_count >= 0
    ),
    not_before TEXT,
    lease_token TEXT CHECK (lease_token IS NULL OR length(lease_token) BETWEEN 16 AND 120),
    leased_at TEXT,
    remote_entry_id INTEGER,
    last_error TEXT NOT NULL DEFAULT '' CHECK (length(last_error) <= 240),
    last_attempt_at TEXT,
    synced_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (
        (sync_state = 'leased' AND lease_token IS NOT NULL AND leased_at IS NOT NULL)
        OR sync_state <> 'leased'
    )
);

CREATE TABLE IF NOT EXISTS watch_integrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (source IN ('apple_tv')),
    key_id TEXT NOT NULL UNIQUE CHECK (length(key_id) BETWEEN 16 AND 128),
    encrypted_secret TEXT NOT NULL CHECK (length(encrypted_secret) BETWEEN 1 AND 4096),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (
        typeof(enabled) = 'integer' AND enabled IN (0, 1)
    ),
    last_seen_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, source)
);

CREATE INDEX IF NOT EXISTS idx_watch_entries_user_status
    ON watch_entries(user_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_watch_events_entry_created
    ON watch_events(watch_entry_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_watch_confirmations_pending
    ON watch_confirmations(user_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tracker_sync_outbox_retry
    ON tracker_sync_outbox(sync_state, not_before, created_at, id);
CREATE INDEX IF NOT EXISTS idx_watch_integrations_user_enabled
    ON watch_integrations(user_id, source, enabled);

CREATE TRIGGER IF NOT EXISTS trg_watch_entries_owner_insert
BEFORE INSERT ON watch_entries
WHEN NEW.monitor_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM media_monitors AS monitor
        WHERE monitor.id = NEW.monitor_id AND monitor.user_id = NEW.user_id
    ) THEN RAISE(ABORT, 'watch entry owner mismatch') END;
END;

CREATE TRIGGER IF NOT EXISTS trg_watch_entries_owner_update
BEFORE UPDATE OF user_id, monitor_id ON watch_entries
WHEN NEW.monitor_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM media_monitors AS monitor
        WHERE monitor.id = NEW.monitor_id AND monitor.user_id = NEW.user_id
    ) THEN RAISE(ABORT, 'watch entry owner mismatch') END;
END;

CREATE TRIGGER IF NOT EXISTS trg_watch_events_owner_insert
BEFORE INSERT ON watch_events
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM watch_entries AS entry
        WHERE entry.id = NEW.watch_entry_id AND entry.user_id = NEW.user_id
    ) THEN RAISE(ABORT, 'watch event owner mismatch') END;
END;

CREATE TRIGGER IF NOT EXISTS trg_watch_events_immutable
BEFORE UPDATE ON watch_events
BEGIN
    SELECT RAISE(ABORT, 'watch events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_watch_confirmations_owner_insert
BEFORE INSERT ON watch_confirmations
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM watch_events AS event
        JOIN watch_entries AS entry ON entry.id = event.watch_entry_id
        WHERE event.id = NEW.watch_event_id
          AND event.watch_entry_id = NEW.watch_entry_id
          AND event.user_id = NEW.user_id
          AND entry.user_id = NEW.user_id
    ) THEN RAISE(ABORT, 'watch confirmation owner mismatch') END;
END;

CREATE TRIGGER IF NOT EXISTS trg_watch_confirmations_owner_update
BEFORE UPDATE OF user_id, watch_entry_id, watch_event_id ON watch_confirmations
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM watch_events AS event
        JOIN watch_entries AS entry ON entry.id = event.watch_entry_id
        WHERE event.id = NEW.watch_event_id
          AND event.watch_entry_id = NEW.watch_entry_id
          AND event.user_id = NEW.user_id
          AND entry.user_id = NEW.user_id
    ) THEN RAISE(ABORT, 'watch confirmation owner mismatch') END;
END;

CREATE TRIGGER IF NOT EXISTS trg_tracker_sync_outbox_owner_insert
BEFORE INSERT ON tracker_sync_outbox
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM watch_events AS event
        JOIN watch_entries AS entry ON entry.id = event.watch_entry_id
        WHERE event.id = NEW.watch_event_id
          AND event.watch_entry_id = NEW.watch_entry_id
          AND event.user_id = NEW.user_id
          AND entry.user_id = NEW.user_id
    ) THEN RAISE(ABORT, 'tracker outbox owner mismatch') END;
END;

CREATE TRIGGER IF NOT EXISTS trg_tracker_sync_outbox_owner_update
BEFORE UPDATE OF user_id, watch_entry_id, watch_event_id ON tracker_sync_outbox
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM watch_events AS event
        JOIN watch_entries AS entry ON entry.id = event.watch_entry_id
        WHERE event.id = NEW.watch_event_id
          AND event.watch_entry_id = NEW.watch_entry_id
          AND event.user_id = NEW.user_id
          AND entry.user_id = NEW.user_id
    ) THEN RAISE(ABORT, 'tracker outbox owner mismatch') END;
END;

INSERT OR IGNORE INTO watch_entries (
    user_id,
    monitor_id,
    anilist_id,
    season_number,
    status,
    progress_episodes,
    episode_total,
    last_source,
    confidence,
    completed_at
)
SELECT
    monitor.user_id,
    monitor.id,
    monitor.anilist_id,
    COALESCE(monitor.season_number, 0),
    CASE
        WHEN UPPER(COALESCE(planning.status, '')) = 'COMPLETED'
          OR EXISTS (
              SELECT 1 FROM tracker_media_actions AS action
              WHERE action.user_id = monitor.user_id
                AND action.media_id = monitor.anilist_id
                AND action.action = 'completed'
          )
        THEN 'completed'
        ELSE 'unwatched'
    END,
    CASE
        WHEN (
            UPPER(COALESCE(planning.status, '')) = 'COMPLETED'
            OR EXISTS (
                SELECT 1 FROM tracker_media_actions AS action
                WHERE action.user_id = monitor.user_id
                  AND action.media_id = monitor.anilist_id
                  AND action.action = 'completed'
            )
        ) AND monitor.expected_episodes > 0
        THEN monitor.expected_episodes
        ELSE 0
    END,
    CASE WHEN monitor.expected_episodes > 0 THEN monitor.expected_episodes END,
    'migration',
    'manual',
    CASE
        WHEN UPPER(COALESCE(planning.status, '')) = 'COMPLETED'
          OR EXISTS (
              SELECT 1 FROM tracker_media_actions AS action
              WHERE action.user_id = monitor.user_id
                AND action.media_id = monitor.anilist_id
                AND action.action = 'completed'
          )
        THEN datetime('now')
    END
FROM media_monitors AS monitor
LEFT JOIN anilist_plannings AS planning ON planning.id = monitor.anilist_planning_id;
