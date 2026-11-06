-- Preserve entry identity and all referencing household rows. SQLite 3.35+ is required.
ALTER TABLE watch_entries ADD COLUMN web_source_038 TEXT;
UPDATE watch_entries SET web_source_038=last_source;
ALTER TABLE watch_entries DROP COLUMN last_source;
ALTER TABLE watch_entries ADD COLUMN last_source TEXT NOT NULL DEFAULT 'manual'
 CHECK(last_source IN ('plex','apple_tv','anilist','manual','migration','web_player'));
UPDATE watch_entries SET last_source=web_source_038;
ALTER TABLE watch_entries DROP COLUMN web_source_038;

-- The runner owns one atomic transaction with foreign keys enabled. Preserve
-- child rows before rebuilding their referenced event table, without cascades.
CREATE TEMP TABLE web_sequences_038 AS SELECT name,seq FROM sqlite_sequence
 WHERE name IN ('watch_events','watch_confirmations','tracker_sync_outbox');

CREATE TEMP TABLE web_watch_events_038 AS SELECT * FROM watch_events;

CREATE TEMP TABLE web_watch_confirmations_038 AS SELECT * FROM watch_confirmations;

CREATE TEMP TABLE web_tracker_sync_outbox_038 AS SELECT * FROM tracker_sync_outbox;

DROP TABLE watch_confirmations;

DROP TABLE tracker_sync_outbox;

DROP TABLE watch_events;

CREATE TABLE watch_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_entry_id INTEGER NOT NULL REFERENCES watch_entries(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (
        source IN ('plex', 'apple_tv', 'anilist', 'manual', 'migration', 'web_player')
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
    score_ten INTEGER CHECK (score_ten IS NULL OR (typeof(score_ten) = 'integer' AND score_ten BETWEEN 1 AND 10 AND source = 'manual' AND kind = 'completed')),
    UNIQUE (user_id, source, event_key)
);

INSERT INTO watch_events (id,watch_entry_id,user_id,source,event_key,kind,episode,playback_fraction,confidence,force,disposition,reason,observed_at,payload_json,created_at,score_ten) SELECT id,watch_entry_id,user_id,source,event_key,kind,episode,playback_fraction,confidence,force,disposition,reason,observed_at,payload_json,created_at,score_ten FROM web_watch_events_038;
DROP TABLE web_watch_events_038;

CREATE TABLE watch_confirmations (
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

INSERT INTO watch_confirmations (id,watch_entry_id,watch_event_id,user_id,status,proposed_episode,prompt,resolved_at,created_at,updated_at) SELECT id,watch_entry_id,watch_event_id,user_id,status,proposed_episode,prompt,resolved_at,created_at,updated_at FROM web_watch_confirmations_038;
DROP TABLE web_watch_confirmations_038;

CREATE TABLE tracker_sync_outbox (
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
    score_ten INTEGER CHECK (score_ten IS NULL OR (typeof(score_ten) = 'integer' AND score_ten BETWEEN 1 AND 10 AND action = 'complete')),
    CHECK (
        (sync_state = 'leased' AND lease_token IS NOT NULL AND leased_at IS NOT NULL)
        OR sync_state <> 'leased'
    )
);

INSERT INTO tracker_sync_outbox (id,watch_entry_id,watch_event_id,user_id,tracker,action,progress_episodes,sync_state,attempt_count,not_before,lease_token,leased_at,remote_entry_id,last_error,last_attempt_at,synced_at,created_at,updated_at,score_ten) SELECT id,watch_entry_id,watch_event_id,user_id,tracker,action,progress_episodes,sync_state,attempt_count,not_before,lease_token,leased_at,remote_entry_id,last_error,last_attempt_at,synced_at,created_at,updated_at,score_ten FROM web_tracker_sync_outbox_038;
DROP TABLE web_tracker_sync_outbox_038;

CREATE INDEX IF NOT EXISTS idx_watch_events_entry_created
    ON watch_events(watch_entry_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_watch_confirmations_pending
    ON watch_confirmations(user_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_tracker_sync_outbox_retry
    ON tracker_sync_outbox(sync_state, not_before, created_at, id);

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

CREATE INDEX idx_notification_anime_score
 ON watch_events(watch_entry_id,user_id,id DESC)
 WHERE disposition='accepted' AND score_ten IS NOT NULL;

UPDATE sqlite_sequence SET seq=MAX(seq,COALESCE((SELECT old.seq FROM web_sequences_038 old WHERE old.name=sqlite_sequence.name),seq))
 WHERE name IN ('watch_events','watch_confirmations','tracker_sync_outbox');
INSERT INTO sqlite_sequence(name,seq) SELECT old.name,old.seq FROM web_sequences_038 old
 WHERE NOT EXISTS(SELECT 1 FROM sqlite_sequence current WHERE current.name=old.name);
DROP TABLE web_sequences_038;
