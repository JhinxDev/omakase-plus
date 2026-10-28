-- Language contracts are additive here. Root owns the coordinated movie-proof
-- rebuild section; SQL023 and existing verification rows remain untouched.
CREATE TABLE library_language_defaults (
 library_id TEXT PRIMARY KEY REFERENCES libraries(id) ON DELETE RESTRICT,
 policies_json TEXT NOT NULL CHECK(json_valid(policies_json) AND length(policies_json)<=16384),
 revision TEXT NOT NULL CHECK(length(revision)=64),
 allow_member_preferences INTEGER NOT NULL DEFAULT 1 CHECK(allow_member_preferences IN(0,1)),
 allow_request_overrides INTEGER NOT NULL DEFAULT 1 CHECK(allow_request_overrides IN(0,1)),
 updated_at TEXT NOT NULL
);
CREATE TABLE language_request_policies (
 id TEXT PRIMARY KEY CHECK(length(id) BETWEEN 1 AND 80),
 actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE RESTRICT,
 library_item_id TEXT NOT NULL REFERENCES library_items(id) ON DELETE RESTRICT,
 resource_id TEXT REFERENCES library_resources(id) ON DELETE RESTRICT,
 household_event_id TEXT REFERENCES library_request_events(id) ON DELETE RESTRICT,
 domain TEXT NOT NULL CHECK(domain IN('anime','movie','tv')),
 intent TEXT NOT NULL CHECK(intent IN('evaluate','acquire')),
 snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json) AND length(snapshot_json)<=16384),
 acquisition_json TEXT NOT NULL CHECK(json_valid(acquisition_json) AND length(acquisition_json)<=16384),
 publication_policy_json TEXT NOT NULL CHECK(json_valid(publication_policy_json) AND length(publication_policy_json)<=8192),
 publication_policy_revision TEXT NOT NULL CHECK(length(publication_policy_revision)=64),
 required_episodes_json TEXT NOT NULL CHECK(json_valid(required_episodes_json) AND json_type(required_episodes_json)='array' AND length(required_episodes_json)<=8192),
 policy_revision TEXT NOT NULL CHECK(length(policy_revision)=64),
 binding_digest TEXT NOT NULL CHECK(length(binding_digest)=64),
 request_digest TEXT NOT NULL CHECK(length(request_digest)=64),
 membership_revision INTEGER NOT NULL,
 library_policy_revision INTEGER NOT NULL,
 created_at TEXT NOT NULL
);
CREATE INDEX language_requests_actor ON language_request_policies(actor_user_id,library_id,id);
CREATE TRIGGER language_request_scope BEFORE INSERT ON language_request_policies
WHEN NOT EXISTS(SELECT 1 FROM library_items i JOIN libraries l ON l.id=i.library_id
 WHERE i.id=NEW.library_item_id AND i.library_id=NEW.library_id AND i.domain=NEW.domain AND l.state='active'
 AND (NEW.resource_id IS NULL OR i.resource_id=NEW.resource_id)
 AND ((l.kind='personal' AND l.owner_user_id=NEW.actor_user_id) OR
 (l.kind='household' AND EXISTS(SELECT 1 FROM household_memberships m WHERE m.library_id=l.id AND m.user_id=NEW.actor_user_id AND m.state='active'))))
BEGIN SELECT RAISE(ABORT,'Language request scope mismatch'); END;
CREATE TRIGGER language_request_immutable BEFORE UPDATE ON language_request_policies
BEGIN SELECT RAISE(ABORT,'Language request snapshot is immutable'); END;
CREATE TABLE language_request_bindings (
 request_id TEXT PRIMARY KEY REFERENCES language_request_policies(id) ON DELETE RESTRICT,
 resource_id TEXT NOT NULL REFERENCES library_resources(id) ON DELETE RESTRICT,
 bound_at TEXT NOT NULL
);
CREATE TRIGGER language_binding_matches_item BEFORE INSERT ON language_request_bindings
WHEN NOT EXISTS(SELECT 1 FROM language_request_policies p JOIN library_items i ON i.id=p.library_item_id
 WHERE p.id=NEW.request_id AND i.resource_id=NEW.resource_id AND (p.resource_id IS NULL OR p.resource_id=NEW.resource_id))
BEGIN SELECT RAISE(ABORT,'Language resource binding mismatch'); END;
CREATE TRIGGER language_binding_immutable BEFORE UPDATE ON language_request_bindings
BEGIN SELECT RAISE(ABORT,'Language resource binding is immutable'); END;
CREATE TABLE asset_language_observations (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 resource_id TEXT NOT NULL REFERENCES library_resources(id) ON DELETE RESTRICT,
 observer_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 asset_id INTEGER REFERENCES media_assets(id) ON DELETE RESTRICT,
 probe_id INTEGER REFERENCES media_file_probes(id) ON DELETE RESTRICT,
 file_fingerprint TEXT NOT NULL CHECK(length(file_fingerprint)=64),
 evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json) AND length(evidence_json)<=131072),
 inventory_generation TEXT NOT NULL CHECK(length(inventory_generation) BETWEEN 1 AND 128),
 episodes_json TEXT NOT NULL CHECK(json_valid(episodes_json) AND json_type(episodes_json)='array' AND length(episodes_json)<=8192),
 source_generation TEXT NOT NULL CHECK(length(source_generation) BETWEEN 1 AND 128),
 observation_digest TEXT NOT NULL CHECK(length(observation_digest)=64),
 observed_at TEXT NOT NULL,
 UNIQUE(resource_id,observation_digest)
);
CREATE INDEX language_observations_resource_file ON asset_language_observations(resource_id,file_fingerprint,id);
CREATE TRIGGER language_observer_owner BEFORE INSERT ON asset_language_observations
WHEN NOT EXISTS(SELECT 1 FROM library_resources r WHERE r.id=NEW.resource_id AND r.owner_user_id=NEW.observer_user_id)
BEGIN SELECT RAISE(ABORT,'Language observation owner mismatch'); END;
CREATE TRIGGER language_observation_immutable BEFORE UPDATE ON asset_language_observations
BEGIN SELECT RAISE(ABORT,'Language observation is immutable'); END;
CREATE TABLE language_evaluations (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 request_id TEXT NOT NULL REFERENCES language_request_policies(id) ON DELETE RESTRICT,
 observation_id INTEGER NOT NULL REFERENCES asset_language_observations(id) ON DELETE RESTRICT,
 policy_revision TEXT NOT NULL CHECK(length(policy_revision)=64),
 file_fingerprint TEXT NOT NULL CHECK(length(file_fingerprint)=64),
 playback_context TEXT NOT NULL DEFAULT '' CHECK(length(playback_context)<=128),
 state TEXT NOT NULL CHECK(state IN('unsupported','pending_verification','language_unknown','language_unavailable','language_compatible','fallback_available','selection_required','selection_unconfirmed')),
 physical_compatible INTEGER NOT NULL CHECK(physical_compatible IN(0,1)),
 selection_confirmed INTEGER NOT NULL CHECK(selection_confirmed IN(0,1)),
 evaluation_json TEXT NOT NULL CHECK(json_valid(evaluation_json) AND length(evaluation_json)<=32768),
 evaluated_at TEXT NOT NULL,
 UNIQUE(request_id,observation_id,playback_context)
);
CREATE TRIGGER language_evaluation_binding BEFORE INSERT ON language_evaluations
WHEN NOT EXISTS(SELECT 1 FROM language_request_policies p JOIN language_request_bindings b ON b.request_id=p.id
 JOIN asset_language_observations o ON o.resource_id=b.resource_id
 WHERE p.id=NEW.request_id AND o.id=NEW.observation_id AND p.policy_revision=NEW.policy_revision AND o.file_fingerprint=NEW.file_fingerprint)
BEGIN SELECT RAISE(ABORT,'Language evaluation binding mismatch'); END;
CREATE TRIGGER language_evaluation_immutable BEFORE UPDATE ON language_evaluations
BEGIN SELECT RAISE(ABORT,'Language evaluation is immutable'); END;
CREATE TABLE publication_language_contracts (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 resource_id TEXT NOT NULL REFERENCES library_resources(id) ON DELETE RESTRICT,
 asset_id INTEGER NOT NULL REFERENCES media_assets(id) ON DELETE RESTRICT,
 approved_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 policy_json TEXT NOT NULL CHECK(json_valid(policy_json) AND length(policy_json)<=8192),
 policy_revision TEXT NOT NULL CHECK(length(policy_revision)=64),
 contract_version TEXT NOT NULL CHECK(contract_version='language-policy-v1'),
 created_at TEXT NOT NULL,
 UNIQUE(resource_id,asset_id)
);
CREATE TRIGGER language_publication_owner BEFORE INSERT ON publication_language_contracts
WHEN NOT EXISTS(SELECT 1 FROM library_resources r JOIN media_monitors m ON CAST(m.id AS TEXT)=r.legacy_id AND m.user_id=r.owner_user_id
 JOIN media_assets a ON a.monitor_id=m.id WHERE r.id=NEW.resource_id AND r.domain='anime' AND r.owner_user_id=NEW.approved_by AND a.id=NEW.asset_id)
BEGIN SELECT RAISE(ABORT,'Publication language contract owner mismatch'); END;
CREATE TRIGGER language_publication_immutable BEFORE UPDATE ON publication_language_contracts
BEGIN SELECT RAISE(ABORT,'Publication language contract is immutable'); END;

CREATE TABLE language_request_assets (
 request_id TEXT NOT NULL REFERENCES language_request_policies(id) ON DELETE RESTRICT,
 asset_id INTEGER NOT NULL REFERENCES media_assets(id) ON DELETE RESTRICT,
 created_at TEXT NOT NULL,
 PRIMARY KEY(request_id,asset_id)
);
-- Preserve the pre-language monitor contract. Future preference saves do not
-- silently rewrite these already-approved legacy English publication jobs.
CREATE TABLE legacy_language_monitors (
 monitor_id INTEGER PRIMARY KEY REFERENCES media_monitors(id) ON DELETE CASCADE,
 registered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
INSERT INTO legacy_language_monitors(monitor_id) SELECT id FROM media_monitors;

-- Authoritative current projection is updated only by explicit trusted writers.
CREATE TABLE language_current_sources (
 library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE RESTRICT,
 resource_id TEXT NOT NULL REFERENCES library_resources(id) ON DELETE RESTRICT,
 owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 integration TEXT NOT NULL CHECK(integration IN('plex','realdebrid','local')),
 source_generation TEXT NOT NULL CHECK(length(source_generation)=64),
 library_generation INTEGER NOT NULL CHECK(library_generation>=0),
 PRIMARY KEY(library_id,resource_id)
);
CREATE TABLE language_request_sources (
 request_id TEXT PRIMARY KEY REFERENCES language_request_policies(id) ON DELETE RESTRICT,
 source_generation TEXT NOT NULL CHECK(length(source_generation)=64)
);
CREATE TRIGGER language_request_source_immutable BEFORE UPDATE ON language_request_sources
BEGIN SELECT RAISE(ABORT,'Request source binding is immutable'); END;
CREATE TABLE viewer_playback_bindings (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 external_account_id TEXT NOT NULL,
 credential_generation TEXT NOT NULL CHECK(length(credential_generation)=64),
 server_id TEXT NOT NULL,
 source_generation TEXT NOT NULL CHECK(length(source_generation)=64),
 verified_at TEXT NOT NULL,
 expires_at TEXT NOT NULL
);
CREATE TRIGGER viewer_binding_immutable BEFORE UPDATE ON viewer_playback_bindings
BEGIN SELECT RAISE(ABORT,'Verified viewer binding is immutable'); END;
CREATE TABLE movie_file_observations (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 resource_id TEXT NOT NULL REFERENCES library_resources(id) ON DELETE RESTRICT,
 imdb_id TEXT NOT NULL,
 server_id TEXT NOT NULL,
 section_id TEXT NOT NULL,
 rating_key TEXT NOT NULL,
 media_id TEXT NOT NULL,
 part_id TEXT NOT NULL,
 physical_fingerprint TEXT NOT NULL CHECK(length(physical_fingerprint)=64),
 legacy_fingerprint TEXT NOT NULL CHECK(length(legacy_fingerprint)=64),
 resolution TEXT,
 edition TEXT,
 video_codec TEXT NOT NULL,
 source_generation TEXT NOT NULL CHECK(length(source_generation)=64),
 language_observation_id INTEGER NOT NULL REFERENCES asset_language_observations(id) ON DELETE RESTRICT,
 observed_at TEXT NOT NULL,
 expires_at TEXT NOT NULL
);
CREATE TRIGGER movie_file_immutable BEFORE UPDATE ON movie_file_observations
BEGIN SELECT RAISE(ABORT,'Movie file observation is immutable'); END;
CREATE TABLE movie_byte_observations (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 file_observation_id INTEGER NOT NULL REFERENCES movie_file_observations(id) ON DELETE RESTRICT,
 http_status INTEGER NOT NULL CHECK(http_status=206),
 bytes_read INTEGER NOT NULL CHECK(bytes_read=65536),
 observed_at TEXT NOT NULL,
 expires_at TEXT NOT NULL
);
CREATE TRIGGER movie_bytes_immutable BEFORE UPDATE ON movie_byte_observations
BEGIN SELECT RAISE(ABORT,'Movie byte observation is immutable'); END;
CREATE TABLE viewer_selection_observations (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 binding_id INTEGER NOT NULL REFERENCES viewer_playback_bindings(id) ON DELETE RESTRICT,
 file_observation_id INTEGER NOT NULL REFERENCES movie_file_observations(id) ON DELETE RESTRICT,
 byte_observation_id INTEGER NOT NULL REFERENCES movie_byte_observations(id) ON DELETE RESTRICT,
 selected_audio_id TEXT,
 selected_subtitle_id TEXT,
 observed_at TEXT NOT NULL,
 expires_at TEXT NOT NULL
);
CREATE TRIGGER viewer_selection_immutable BEFORE UPDATE ON viewer_selection_observations
BEGIN SELECT RAISE(ABORT,'Viewer selection observation is immutable'); END;
ALTER TABLE language_evaluations ADD COLUMN playback_selection_id INTEGER REFERENCES viewer_selection_observations(id) ON DELETE RESTRICT;

CREATE TRIGGER viewer_external_identity_unique_actor BEFORE INSERT ON viewer_playback_bindings
WHEN EXISTS(SELECT 1 FROM viewer_playback_bindings b WHERE b.server_id=NEW.server_id AND b.external_account_id=NEW.external_account_id AND b.actor_user_id!=NEW.actor_user_id)
BEGIN SELECT RAISE(ABORT,'External playback identity already belongs to another local viewer'); END;

CREATE TABLE viewer_current_playback_credentials (
 actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 server_id TEXT NOT NULL,
 credential_generation TEXT NOT NULL CHECK(length(credential_generation)=64),
 PRIMARY KEY(actor_user_id,server_id)
);

-- Preferences are account/library choices only; no physical evidence is stored here.
CREATE TABLE member_language_preferences (
    library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    domain TEXT NOT NULL CHECK(domain IN ('anime','movie','tv')),
    rule_json TEXT NOT NULL,
    revision TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(library_id,user_id,domain)
);

-- Current applicability only; historical language observations remain immutable.
CREATE TABLE language_publication_current_files (
 asset_id INTEGER NOT NULL REFERENCES media_assets(id) ON DELETE RESTRICT,
 file_locator TEXT NOT NULL,
 observation_id INTEGER REFERENCES asset_language_observations(id) ON DELETE RESTRICT,
 curator_run_id TEXT NOT NULL,
 completed_at TEXT NOT NULL,
 PRIMARY KEY(asset_id,file_locator)
);

CREATE TRIGGER language_publication_file_insert BEFORE INSERT ON language_publication_current_files
WHEN NEW.observation_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM asset_language_observations o JOIN media_file_probes p ON p.id=o.probe_id WHERE o.id=NEW.observation_id AND o.asset_id=NEW.asset_id AND p.asset_id=NEW.asset_id AND p.file_locator=NEW.file_locator)
BEGIN SELECT RAISE(ABORT,'Current publication file binding mismatch'); END;

CREATE TRIGGER language_publication_file_update BEFORE UPDATE ON language_publication_current_files
WHEN NEW.observation_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM asset_language_observations o JOIN media_file_probes p ON p.id=o.probe_id WHERE o.id=NEW.observation_id AND o.asset_id=NEW.asset_id AND p.asset_id=NEW.asset_id AND p.file_locator=NEW.file_locator)
BEGIN SELECT RAISE(ABORT,'Current publication file binding mismatch'); END;


-- A new explicit check invalidates prior applicability even if HTTP fails.
-- Historical positive observations are retained; only this projection changes.
CREATE TABLE viewer_current_playback_checks (
 actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 resource_id TEXT NOT NULL REFERENCES library_resources(id) ON DELETE RESTRICT,
 nonce TEXT NOT NULL CHECK(length(nonce)=32),
 selection_id INTEGER REFERENCES viewer_selection_observations(id) ON DELETE RESTRICT,
 started_at TEXT NOT NULL,
 PRIMARY KEY(actor_user_id,resource_id)
);
