-- Preserve every original receipt column; schema1 keeps the original English gate.
-- Schema2 adds an exact policy/file/selected-stream applicability proof.
CREATE TABLE movie_verifications_next (
 user_id INTEGER NOT NULL,
 imdb_id TEXT NOT NULL,
 fingerprint TEXT NOT NULL CHECK(length(fingerprint)=64),
 plex_key TEXT NOT NULL,
 resolution TEXT,
 selected_audio TEXT,
 edition TEXT,
 state TEXT NOT NULL CHECK(state IN('ready','verifying','deferred','unavailable')),
 code TEXT NOT NULL,
 detail TEXT NOT NULL,
 http_status INTEGER,
 bytes_read INTEGER NOT NULL DEFAULT 0 CHECK(bytes_read>=0),
 verified_at TEXT NOT NULL,
 expires_at TEXT NOT NULL,
 retry_after TEXT,
 retry_at TEXT,
 proof_schema INTEGER NOT NULL DEFAULT 1 CHECK(proof_schema IN(1,2)),
 language_evaluation_id INTEGER REFERENCES language_evaluations(id) ON DELETE RESTRICT,
 physical_fingerprint TEXT CHECK(physical_fingerprint IS NULL OR length(physical_fingerprint)=64),
 selected_stream_id TEXT CHECK(selected_stream_id IS NULL OR length(selected_stream_id) BETWEEN 1 AND 64),
 playback_context TEXT CHECK(playback_context IS NULL OR length(playback_context) BETWEEN 1 AND 128),
 source_generation TEXT CHECK(source_generation IS NULL OR length(source_generation) BETWEEN 1 AND 128),
 playback_selection_id INTEGER REFERENCES viewer_selection_observations(id) ON DELETE RESTRICT,
 movie_file_observation_id INTEGER REFERENCES movie_file_observations(id) ON DELETE RESTRICT,
 movie_byte_observation_id INTEGER REFERENCES movie_byte_observations(id) ON DELETE RESTRICT,
 selected_subtitle_id TEXT,
 CHECK(state!='ready' OR (http_status IS 206 AND bytes_read=65536 AND code='ready' AND
   ((proof_schema=1 AND selected_audio='English') OR
    (proof_schema=2 AND language_evaluation_id IS NOT NULL AND physical_fingerprint IS NOT NULL
     AND selected_stream_id IS NOT NULL AND playback_context IS NOT NULL AND source_generation IS NOT NULL AND playback_selection_id IS NOT NULL AND movie_file_observation_id IS NOT NULL AND movie_byte_observation_id IS NOT NULL)))),
 PRIMARY KEY(user_id,imdb_id,fingerprint),
 FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
INSERT INTO movie_verifications_next (
 user_id,imdb_id,fingerprint,plex_key,resolution,selected_audio,edition,state,code,detail,
 http_status,bytes_read,verified_at,expires_at,retry_after,retry_at
) SELECT user_id,imdb_id,fingerprint,plex_key,resolution,selected_audio,edition,state,code,detail,
 http_status,bytes_read,verified_at,expires_at,retry_after,retry_at FROM movie_verifications;
DROP TABLE movie_verifications;
ALTER TABLE movie_verifications_next RENAME TO movie_verifications;
CREATE INDEX idx_movie_verifications_user_state ON movie_verifications(user_id,state);

CREATE TRIGGER movie_language_ready_insert BEFORE INSERT ON movie_verifications
WHEN NEW.state='ready' AND NEW.proof_schema=2 AND NOT EXISTS (
 SELECT 1 FROM language_evaluations e
 JOIN language_request_policies p ON p.id=e.request_id
 JOIN language_request_bindings rb ON rb.request_id=p.id
 JOIN language_request_sources rs ON rs.request_id=p.id
 JOIN library_items i ON i.id=p.library_item_id AND i.library_id=p.library_id AND i.resource_id=rb.resource_id
 JOIN libraries l ON l.id=p.library_id
 JOIN library_resources r ON r.id=rb.resource_id
 JOIN movie_catalog catalog ON catalog.user_id=r.owner_user_id AND catalog.imdb_id=r.canonical_id AND catalog.identity_verified=1
 JOIN language_current_sources c ON c.library_id=l.id AND c.resource_id=r.id
 JOIN asset_language_observations o ON o.id=e.observation_id AND o.resource_id=r.id
 JOIN viewer_selection_observations v ON v.id=e.playback_selection_id
 JOIN viewer_playback_bindings b ON b.id=v.binding_id
 JOIN viewer_current_playback_credentials credentials ON credentials.actor_user_id=b.actor_user_id AND credentials.server_id=b.server_id AND credentials.credential_generation=b.credential_generation
 JOIN movie_file_observations f ON f.id=v.file_observation_id AND f.language_observation_id=o.id
 JOIN movie_byte_observations y ON y.id=v.byte_observation_id AND y.file_observation_id=f.id
 WHERE e.id=NEW.language_evaluation_id AND p.actor_user_id=NEW.user_id AND p.domain='movie'
 AND json_extract(p.snapshot_json,'$.media_identity')='imdb:'||NEW.imdb_id
 AND r.domain='movie' AND r.canonical_id=NEW.imdb_id AND i.domain='movie' AND i.canonical_id=NEW.imdb_id
 AND l.state='active' AND i.state='active'
 AND ((l.kind='personal' AND l.owner_user_id=NEW.user_id) OR (l.kind='household' AND EXISTS(SELECT 1 FROM household_memberships h WHERE h.library_id=l.id AND h.user_id=NEW.user_id AND h.state='active')))
 AND p.membership_revision=l.membership_revision AND p.library_policy_revision=l.policy_revision
 AND c.owner_user_id=r.owner_user_id AND c.integration='plex'
 AND (l.kind='personal' OR EXISTS(SELECT 1 FROM library_settings g WHERE g.library_id=l.id AND g.integration='plex' AND g.owner_user_id=r.owner_user_id AND g.generation=c.library_generation))
 AND c.source_generation=rs.source_generation AND c.source_generation=o.source_generation
 AND c.source_generation=f.source_generation AND c.source_generation=b.source_generation AND c.source_generation=NEW.source_generation
 AND b.actor_user_id=NEW.user_id AND b.server_id=f.server_id
 AND f.resource_id=r.id AND f.imdb_id=NEW.imdb_id AND f.rating_key=NEW.plex_key
 AND f.physical_fingerprint=o.file_fingerprint AND f.physical_fingerprint=e.file_fingerprint AND f.physical_fingerprint=NEW.physical_fingerprint
 AND f.legacy_fingerprint=NEW.fingerprint
 AND v.id=NEW.playback_selection_id AND f.id=NEW.movie_file_observation_id AND y.id=NEW.movie_byte_observation_id
 AND EXISTS(SELECT 1 FROM viewer_current_playback_checks latest WHERE latest.actor_user_id=b.actor_user_id AND latest.resource_id=f.resource_id AND latest.selection_id=v.id)
 AND y.http_status=206 AND y.bytes_read=65536 AND NEW.http_status=206 AND NEW.bytes_read=65536
 AND v.selected_audio_id=NEW.selected_stream_id AND v.selected_subtitle_id IS NEW.selected_subtitle_id
 AND NOT EXISTS(SELECT 1 FROM viewer_selection_observations newer
   JOIN viewer_playback_bindings nb ON nb.id=newer.binding_id
   JOIN movie_file_observations nf ON nf.id=newer.file_observation_id
   WHERE newer.id>v.id AND nb.actor_user_id=b.actor_user_id AND nb.server_id=b.server_id
   AND nf.resource_id=f.resource_id)
 AND e.playback_context='selection:'||v.id AND NEW.playback_context=e.playback_context
 AND e.policy_revision=p.policy_revision AND e.physical_compatible=1 AND e.selection_confirmed=1
 AND e.state IN('language_compatible','fallback_available')
 AND json_extract(e.evaluation_json,'$.selected_audio_confirmed')=1
 AND (json_array_length(json_extract(p.snapshot_json,'$.policy.subtitle_required'))=0 OR json_extract(e.evaluation_json,'$.selected_subtitle_confirmed')=1)
 AND julianday(b.expires_at)>julianday('now') AND julianday(f.expires_at)>julianday('now') AND julianday(y.expires_at)>julianday('now') AND julianday(v.expires_at)>julianday('now')
)
BEGIN SELECT RAISE(ABORT,'Movie language ready proof is not applicable'); END;
CREATE TRIGGER movie_language_ready_update BEFORE UPDATE ON movie_verifications
WHEN NEW.state='ready' AND NEW.proof_schema=2 AND NOT EXISTS (
 SELECT 1 FROM language_evaluations e
 JOIN language_request_policies p ON p.id=e.request_id
 JOIN language_request_bindings rb ON rb.request_id=p.id
 JOIN language_request_sources rs ON rs.request_id=p.id
 JOIN library_items i ON i.id=p.library_item_id AND i.library_id=p.library_id AND i.resource_id=rb.resource_id
 JOIN libraries l ON l.id=p.library_id
 JOIN library_resources r ON r.id=rb.resource_id
 JOIN movie_catalog catalog ON catalog.user_id=r.owner_user_id AND catalog.imdb_id=r.canonical_id AND catalog.identity_verified=1
 JOIN language_current_sources c ON c.library_id=l.id AND c.resource_id=r.id
 JOIN asset_language_observations o ON o.id=e.observation_id AND o.resource_id=r.id
 JOIN viewer_selection_observations v ON v.id=e.playback_selection_id
 JOIN viewer_playback_bindings b ON b.id=v.binding_id
 JOIN viewer_current_playback_credentials credentials ON credentials.actor_user_id=b.actor_user_id AND credentials.server_id=b.server_id AND credentials.credential_generation=b.credential_generation
 JOIN movie_file_observations f ON f.id=v.file_observation_id AND f.language_observation_id=o.id
 JOIN movie_byte_observations y ON y.id=v.byte_observation_id AND y.file_observation_id=f.id
 WHERE e.id=NEW.language_evaluation_id AND p.actor_user_id=NEW.user_id AND p.domain='movie'
 AND json_extract(p.snapshot_json,'$.media_identity')='imdb:'||NEW.imdb_id
 AND r.domain='movie' AND r.canonical_id=NEW.imdb_id AND i.domain='movie' AND i.canonical_id=NEW.imdb_id
 AND l.state='active' AND i.state='active'
 AND ((l.kind='personal' AND l.owner_user_id=NEW.user_id) OR (l.kind='household' AND EXISTS(SELECT 1 FROM household_memberships h WHERE h.library_id=l.id AND h.user_id=NEW.user_id AND h.state='active')))
 AND p.membership_revision=l.membership_revision AND p.library_policy_revision=l.policy_revision
 AND c.owner_user_id=r.owner_user_id AND c.integration='plex'
 AND (l.kind='personal' OR EXISTS(SELECT 1 FROM library_settings g WHERE g.library_id=l.id AND g.integration='plex' AND g.owner_user_id=r.owner_user_id AND g.generation=c.library_generation))
 AND c.source_generation=rs.source_generation AND c.source_generation=o.source_generation
 AND c.source_generation=f.source_generation AND c.source_generation=b.source_generation AND c.source_generation=NEW.source_generation
 AND b.actor_user_id=NEW.user_id AND b.server_id=f.server_id
 AND f.resource_id=r.id AND f.imdb_id=NEW.imdb_id AND f.rating_key=NEW.plex_key
 AND f.physical_fingerprint=o.file_fingerprint AND f.physical_fingerprint=e.file_fingerprint AND f.physical_fingerprint=NEW.physical_fingerprint
 AND f.legacy_fingerprint=NEW.fingerprint
 AND v.id=NEW.playback_selection_id AND f.id=NEW.movie_file_observation_id AND y.id=NEW.movie_byte_observation_id
 AND EXISTS(SELECT 1 FROM viewer_current_playback_checks latest WHERE latest.actor_user_id=b.actor_user_id AND latest.resource_id=f.resource_id AND latest.selection_id=v.id)
 AND y.http_status=206 AND y.bytes_read=65536 AND NEW.http_status=206 AND NEW.bytes_read=65536
 AND v.selected_audio_id=NEW.selected_stream_id AND v.selected_subtitle_id IS NEW.selected_subtitle_id
 AND NOT EXISTS(SELECT 1 FROM viewer_selection_observations newer
   JOIN viewer_playback_bindings nb ON nb.id=newer.binding_id
   JOIN movie_file_observations nf ON nf.id=newer.file_observation_id
   WHERE newer.id>v.id AND nb.actor_user_id=b.actor_user_id AND nb.server_id=b.server_id
   AND nf.resource_id=f.resource_id)
 AND e.playback_context='selection:'||v.id AND NEW.playback_context=e.playback_context
 AND e.policy_revision=p.policy_revision AND e.physical_compatible=1 AND e.selection_confirmed=1
 AND e.state IN('language_compatible','fallback_available')
 AND json_extract(e.evaluation_json,'$.selected_audio_confirmed')=1
 AND (json_array_length(json_extract(p.snapshot_json,'$.policy.subtitle_required'))=0 OR json_extract(e.evaluation_json,'$.selected_subtitle_confirmed')=1)
 AND julianday(b.expires_at)>julianday('now') AND julianday(f.expires_at)>julianday('now') AND julianday(y.expires_at)>julianday('now') AND julianday(v.expires_at)>julianday('now')
)
BEGIN SELECT RAISE(ABORT,'Movie language ready proof is not applicable'); END;

-- Retain schema1's original own-catalog identity requirement and cascade.
CREATE TRIGGER movie_legacy_catalog_insert BEFORE INSERT ON movie_verifications
WHEN NEW.proof_schema=1 AND NOT EXISTS(SELECT 1 FROM movie_catalog c WHERE c.user_id=NEW.user_id AND c.imdb_id=NEW.imdb_id)
BEGIN SELECT RAISE(ABORT,'Legacy movie receipt requires its own catalog identity'); END;
CREATE TRIGGER movie_legacy_catalog_update BEFORE UPDATE ON movie_verifications
WHEN NEW.proof_schema=1 AND NOT EXISTS(SELECT 1 FROM movie_catalog c WHERE c.user_id=NEW.user_id AND c.imdb_id=NEW.imdb_id)
BEGIN SELECT RAISE(ABORT,'Legacy movie receipt requires its own catalog identity'); END;
CREATE TRIGGER movie_legacy_catalog_delete AFTER DELETE ON movie_catalog
BEGIN DELETE FROM movie_verifications WHERE proof_schema=1 AND user_id=OLD.user_id AND imdb_id=OLD.imdb_id; END;

-- Account/resource supersession lookups stay bounded by indexed proof identities.
CREATE INDEX idx_viewer_selection_binding_file ON viewer_selection_observations(binding_id,file_observation_id,id);
CREATE INDEX idx_viewer_binding_actor_server ON viewer_playback_bindings(actor_user_id,server_id,id);
CREATE INDEX idx_movie_file_resource ON movie_file_observations(resource_id,id);
