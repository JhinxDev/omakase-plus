-- Personal tracker integration only; no household/media/history schema changes.
CREATE TABLE tracker_bridge_connections (
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 provider TEXT NOT NULL CHECK(provider IN ('anilist','myanimelist')),
 remote_user_id INTEGER NOT NULL CHECK(typeof(remote_user_id)='integer' AND remote_user_id>0),
 display_name TEXT NOT NULL CHECK(length(display_name) BETWEEN 1 AND 100),
 generation INTEGER NOT NULL CHECK(generation>0),
 state TEXT NOT NULL CHECK(state IN ('verified','reconnect','disconnected')),
 encrypted_bundle TEXT CHECK(length(encrypted_bundle)<=65536),
 expires_at INTEGER NOT NULL,
 verified_at INTEGER NOT NULL,
 refresh_lease TEXT, refresh_until INTEGER NOT NULL DEFAULT 0,
 retry_at INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(user_id,provider)
);
CREATE UNIQUE INDEX tracker_bridge_remote_owner ON tracker_bridge_connections(provider,remote_user_id) WHERE state!='disconnected';
CREATE TABLE tracker_mal_oauth_states (
 state_hash TEXT PRIMARY KEY CHECK(length(state_hash)=64),
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 session_hash TEXT NOT NULL CHECK(length(session_hash)=64),
 config_hash TEXT NOT NULL CHECK(length(config_hash)=64),
 encrypted_verifier TEXT NOT NULL,
 expires_at INTEGER NOT NULL,
 UNIQUE(user_id)
);
CREATE TABLE tracker_bridge_leases (
 user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 token TEXT NOT NULL,
 expires_at INTEGER NOT NULL
);
CREATE TABLE tracker_bridge_baselines (
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 anilist_id INTEGER NOT NULL CHECK(anilist_id>0),
 mal_id INTEGER NOT NULL CHECK(mal_id>0),
 encrypted_payload TEXT NOT NULL,
 updated_at INTEGER NOT NULL,
 PRIMARY KEY(user_id,anilist_id), UNIQUE(user_id,mal_id)
);
CREATE TABLE tracker_bridge_plans (
 id TEXT PRIMARY KEY CHECK(length(id)=32),
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 plan_hash TEXT NOT NULL CHECK(length(plan_hash)=64),
 encrypted_payload TEXT NOT NULL CHECK(length(encrypted_payload)<=16000000),
 created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'preview' CHECK(state IN ('preview','approved','cancelled')),
 UNIQUE(id,user_id)
);
CREATE INDEX tracker_bridge_plans_owner ON tracker_bridge_plans(user_id,created_at);
CREATE TABLE tracker_bridge_outbox (
 id INTEGER PRIMARY KEY,
 plan_id TEXT NOT NULL,
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 item_key TEXT NOT NULL,
 encrypted_patch TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','leased','acknowledged','conflict','ambiguous','deferred','cancelled')),
 attempt_count INTEGER NOT NULL DEFAULT 0,
 not_before INTEGER NOT NULL DEFAULT 0,
 last_code TEXT NOT NULL DEFAULT '' CHECK(length(last_code)<=100),
 updated_at INTEGER NOT NULL,
 UNIQUE(plan_id,item_key),
 FOREIGN KEY(plan_id,user_id) REFERENCES tracker_bridge_plans(id,user_id) ON DELETE CASCADE
);
CREATE INDEX tracker_bridge_outbox_owner ON tracker_bridge_outbox(user_id,state,not_before);

CREATE TABLE tracker_bridge_scans (
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 provider TEXT NOT NULL CHECK(provider IN ('anilist','myanimelist')),
 generation INTEGER NOT NULL,
 cursor INTEGER,
 complete INTEGER NOT NULL CHECK(complete IN (0,1)),
 encrypted_payload TEXT NOT NULL CHECK(length(encrypted_payload)<=16000000),
 created_at INTEGER NOT NULL,
 PRIMARY KEY(user_id,provider)
);
CREATE TABLE tracker_bridge_id_mappings (
 anilist_id INTEGER PRIMARY KEY CHECK(anilist_id>0),
 mal_id INTEGER NOT NULL UNIQUE CHECK(mal_id>0),
 verified_at INTEGER NOT NULL
);

CREATE TABLE tracker_bridge_preferences (
 user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
 updated_at INTEGER NOT NULL
);

-- Durable bounded scheduler rotation; no provider payload or account credentials.
CREATE TABLE tracker_bridge_worker_state (
 id INTEGER PRIMARY KEY CHECK(id=1),
 last_user_id INTEGER NOT NULL DEFAULT 0,
 lease_token TEXT,
 lease_until INTEGER NOT NULL DEFAULT 0
);
INSERT INTO tracker_bridge_worker_state(id) VALUES(1);
CREATE INDEX tracker_bridge_enabled_accounts ON tracker_bridge_preferences(enabled,user_id);
