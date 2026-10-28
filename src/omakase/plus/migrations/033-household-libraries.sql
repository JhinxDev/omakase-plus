-- Household core only. No provider calls, automatic sharing, or legacy data changes.
CREATE TABLE libraries (
 id TEXT PRIMARY KEY CHECK(length(id)=32),
 kind TEXT NOT NULL CHECK(kind IN ('personal','household')),
 owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
 state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','closed')),
 membership_revision INTEGER NOT NULL DEFAULT 1 CHECK(membership_revision>0),
 policy_revision INTEGER NOT NULL DEFAULT 1 CHECK(policy_revision>0),
 members_can_add INTEGER NOT NULL DEFAULT 1 CHECK(members_can_add IN (0,1)),
 acquisition_requests INTEGER NOT NULL DEFAULT 0 CHECK(acquisition_requests IN (0,1)),
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE UNIQUE INDEX one_personal_library ON libraries(owner_user_id) WHERE kind='personal';
CREATE UNIQUE INDEX one_owned_household ON libraries(owner_user_id) WHERE kind='household' AND state='active';
CREATE TRIGGER active_household_owner_account_delete BEFORE DELETE ON users
WHEN EXISTS(SELECT 1 FROM libraries WHERE owner_user_id=OLD.id AND kind='household' AND state='active')
BEGIN SELECT RAISE(ABORT,'Owner cannot delete account with active household'); END;
CREATE TABLE personal_libraries (
 user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 library_id TEXT NOT NULL UNIQUE REFERENCES libraries(id) ON DELETE RESTRICT
);
INSERT INTO libraries(id,kind,owner_user_id,name) SELECT lower(hex(randomblob(16))),'personal',id,'Personal library' FROM users;
INSERT INTO personal_libraries(user_id,library_id) SELECT owner_user_id,id FROM libraries WHERE kind='personal';
CREATE TABLE household_memberships (
 library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE RESTRICT,
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 role TEXT NOT NULL CHECK(role IN ('owner','member')),
 state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','revoked')),
 display_name TEXT NOT NULL DEFAULT 'Household member' CHECK(length(display_name) BETWEEN 1 AND 80),
 joined_at TEXT NOT NULL,
 revoked_at TEXT,
 PRIMARY KEY(library_id,user_id)
);
CREATE UNIQUE INDEX one_active_household_per_user ON household_memberships(user_id) WHERE state='active';
CREATE TRIGGER household_member_owner_check BEFORE INSERT ON household_memberships
WHEN NOT EXISTS(SELECT 1 FROM libraries WHERE id=NEW.library_id AND kind='household' AND state='active'
 AND ((NEW.role='owner' AND owner_user_id=NEW.user_id) OR (NEW.role='member' AND owner_user_id!=NEW.user_id)))
BEGIN SELECT RAISE(ABORT,'Invalid household role'); END;
CREATE TRIGGER household_member_role_immutable BEFORE UPDATE OF role,library_id,user_id ON household_memberships
WHEN NEW.role!=OLD.role OR NEW.library_id!=OLD.library_id OR NEW.user_id!=OLD.user_id
BEGIN SELECT RAISE(ABORT,'Membership identity is immutable'); END;
CREATE TRIGGER household_owner_cannot_leave BEFORE UPDATE OF state ON household_memberships
WHEN OLD.role='owner' AND NEW.state!='active' AND EXISTS(SELECT 1 FROM libraries WHERE id=OLD.library_id AND state='active')
BEGIN SELECT RAISE(ABORT,'Owner cannot leave active household'); END;
CREATE TRIGGER household_owner_cannot_delete BEFORE DELETE ON household_memberships
WHEN OLD.role='owner' AND EXISTS(SELECT 1 FROM libraries WHERE id=OLD.library_id AND state='active')
BEGIN SELECT RAISE(ABORT,'Owner cannot leave active household'); END;
CREATE TABLE household_invites (
 id TEXT PRIMARY KEY CHECK(length(id)=32),
 library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
 token_digest TEXT NOT NULL UNIQUE CHECK(length(token_digest)=64),
 creator_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 expires_at INTEGER NOT NULL,
 created_at INTEGER NOT NULL,
 consumed_at INTEGER,
 consumed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 revoked_at INTEGER,
 CHECK(expires_at>created_at)
);
CREATE INDEX household_invites_owner_window ON household_invites(library_id,expires_at);
-- Resource identity is independent of membership in any one catalog.
CREATE TABLE library_resources (
 id TEXT PRIMARY KEY CHECK(length(id)=32),
 owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 domain TEXT NOT NULL CHECK(domain IN ('anime','movie','tv')),
 canonical_id TEXT NOT NULL CHECK(length(canonical_id) BETWEEN 1 AND 32),
 season_number INTEGER NOT NULL DEFAULT 0 CHECK(season_number BETWEEN 0 AND 200),
 connector_scope TEXT NOT NULL DEFAULT 'legacy' CHECK(length(connector_scope) BETWEEN 1 AND 80),
 legacy_id TEXT CHECK(legacy_id IS NULL OR length(legacy_id) BETWEEN 1 AND 80),
 UNIQUE(owner_user_id,domain,canonical_id,season_number,connector_scope),
 CHECK((domain='movie' AND length(canonical_id) BETWEEN 9 AND 12 AND substr(canonical_id,1,2)='tt' AND substr(canonical_id,3) NOT GLOB '*[^0-9]*') OR (domain IN ('anime','tv') AND length(canonical_id) BETWEEN 1 AND 10 AND canonical_id NOT GLOB '*[^0-9]*' AND substr(canonical_id,1,1)!='0' AND CAST(canonical_id AS INTEGER)<=2147483647)),
 CHECK(domain='anime' OR season_number=0)
);
CREATE TABLE library_items (
 id TEXT PRIMARY KEY CHECK(length(id)=32),
 state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','removed')),
 library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
 domain TEXT NOT NULL CHECK(domain IN ('anime','movie','tv')),
 canonical_id TEXT NOT NULL CHECK(length(canonical_id) BETWEEN 1 AND 32),
 season_number INTEGER NOT NULL DEFAULT 0 CHECK(season_number BETWEEN 0 AND 200),
 resource_id TEXT REFERENCES library_resources(id) ON DELETE RESTRICT,
 metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metadata_json) AND length(metadata_json)<=20000),
 title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 300),
 created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 created_at TEXT NOT NULL,
 UNIQUE(library_id,domain,canonical_id,season_number),
 CHECK((domain='movie' AND length(canonical_id) BETWEEN 9 AND 12 AND substr(canonical_id,1,2)='tt' AND substr(canonical_id,3) NOT GLOB '*[^0-9]*') OR (domain IN ('anime','tv') AND length(canonical_id) BETWEEN 1 AND 10 AND canonical_id NOT GLOB '*[^0-9]*' AND substr(canonical_id,1,1)!='0' AND CAST(canonical_id AS INTEGER)<=2147483647)),
 CHECK(domain='anime' OR season_number=0)
);
CREATE TRIGGER library_resource_identity_insert BEFORE INSERT ON library_items
WHEN NEW.resource_id IS NOT NULL AND NOT EXISTS(
 SELECT 1 FROM library_resources r JOIN libraries l ON l.id=NEW.library_id
 WHERE r.id=NEW.resource_id AND r.owner_user_id=l.owner_user_id AND r.domain=NEW.domain
 AND r.canonical_id=NEW.canonical_id AND r.season_number=NEW.season_number)
BEGIN SELECT RAISE(ABORT,'Library resource identity mismatch'); END;
CREATE TRIGGER library_resource_identity_update BEFORE UPDATE OF resource_id,library_id,domain,canonical_id,season_number ON library_items
WHEN NEW.resource_id IS NOT NULL AND NOT EXISTS(
 SELECT 1 FROM library_resources r JOIN libraries l ON l.id=NEW.library_id
 WHERE r.id=NEW.resource_id AND r.owner_user_id=l.owner_user_id AND r.domain=NEW.domain
 AND r.canonical_id=NEW.canonical_id AND r.season_number=NEW.season_number)
BEGIN SELECT RAISE(ABORT,'Library resource identity mismatch'); END;
CREATE INDEX library_items_resource_refs ON library_items(resource_id);
CREATE TABLE library_item_interest (
 library_item_id TEXT NOT NULL REFERENCES library_items(id) ON DELETE CASCADE,
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 keep_available INTEGER NOT NULL DEFAULT 0 CHECK(keep_available IN (0,1)),
 active_use INTEGER NOT NULL DEFAULT 0 CHECK(active_use IN (0,1)),
 updated_at TEXT NOT NULL,
 PRIMARY KEY(library_item_id,user_id)
);
CREATE TABLE watch_library_links (
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 library_item_id TEXT NOT NULL REFERENCES library_items(id) ON DELETE CASCADE,
 watch_entry_id INTEGER NOT NULL REFERENCES watch_entries(id) ON DELETE RESTRICT,
 PRIMARY KEY(user_id,library_item_id)
);
CREATE TRIGGER watch_library_link_identity BEFORE INSERT ON watch_library_links
WHEN NOT EXISTS(SELECT 1 FROM watch_entries w JOIN library_items i ON i.id=NEW.library_item_id
 JOIN libraries l ON l.id=i.library_id
 WHERE w.id=NEW.watch_entry_id AND w.user_id=NEW.user_id AND i.domain='anime' AND l.state='active'
 AND CAST(i.canonical_id AS INTEGER)=w.anilist_id AND i.season_number=w.season_number
 AND (l.kind='personal' AND l.owner_user_id=NEW.user_id OR EXISTS(
 SELECT 1 FROM household_memberships m WHERE m.library_id=l.id AND m.user_id=NEW.user_id AND m.state='active')))
BEGIN SELECT RAISE(ABORT,'Unauthorized personal watch link'); END;
CREATE TRIGGER watch_library_link_immutable BEFORE UPDATE ON watch_library_links
BEGIN SELECT RAISE(ABORT,'Watch link identity is immutable'); END;
CREATE TABLE library_request_events (
 next_attempt_at TEXT,
 result_code TEXT NOT NULL DEFAULT 'queued' CHECK(length(result_code) BETWEEN 1 AND 80),
 detail TEXT NOT NULL DEFAULT 'Media request queued.' CHECK(length(detail)<=300),
 id TEXT PRIMARY KEY CHECK(length(id)=32),
 library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
 library_item_id TEXT NOT NULL REFERENCES library_items(id) ON DELETE RESTRICT,
 actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 100),
 state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','dispatched','completed','uncertain','cancelled')),
 membership_revision INTEGER NOT NULL,
 policy_revision INTEGER NOT NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(library_id,actor_user_id,idempotency_key)
);
CREATE TABLE library_settings (
 library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
 integration TEXT NOT NULL CHECK(integration IN ('plex','realdebrid','prowlarr','riven','seerr')),
 owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 generation INTEGER NOT NULL DEFAULT 1 CHECK(generation>0),
 PRIMARY KEY(library_id,integration)
);
CREATE TRIGGER household_connector_owner BEFORE INSERT ON library_settings
WHEN NOT EXISTS(SELECT 1 FROM libraries WHERE id=NEW.library_id AND owner_user_id=NEW.owner_user_id AND kind='household')
BEGIN SELECT RAISE(ABORT,'Shared connector owner mismatch'); END;

CREATE TRIGGER personal_library_owner_insert BEFORE INSERT ON personal_libraries
WHEN NOT EXISTS(SELECT 1 FROM libraries WHERE id=NEW.library_id AND owner_user_id=NEW.user_id AND kind='personal' AND state='active')
BEGIN SELECT RAISE(ABORT,'Personal library owner mismatch'); END;
CREATE TRIGGER personal_library_owner_update BEFORE UPDATE ON personal_libraries
WHEN NOT EXISTS(SELECT 1 FROM libraries WHERE id=NEW.library_id AND owner_user_id=NEW.user_id AND kind='personal' AND state='active')
BEGIN SELECT RAISE(ABORT,'Personal library owner mismatch'); END;
CREATE TRIGGER library_resource_immutable BEFORE UPDATE OF owner_user_id,domain,canonical_id,season_number,connector_scope,legacy_id ON library_resources
WHEN NEW.owner_user_id!=OLD.owner_user_id OR NEW.domain!=OLD.domain OR NEW.canonical_id!=OLD.canonical_id OR NEW.season_number!=OLD.season_number OR NEW.connector_scope!=OLD.connector_scope OR NEW.legacy_id IS NOT OLD.legacy_id
BEGIN SELECT RAISE(ABORT,'Resource identity is immutable'); END;
CREATE TRIGGER library_item_actor_insert BEFORE INSERT ON library_items
WHEN NOT EXISTS(SELECT 1 FROM libraries l WHERE l.id=NEW.library_id AND l.state='active' AND (l.owner_user_id=NEW.created_by OR EXISTS(SELECT 1 FROM household_memberships m WHERE m.library_id=l.id AND m.user_id=NEW.created_by AND m.state='active')))
BEGIN SELECT RAISE(ABORT,'Catalog actor is not authorized'); END;
CREATE TABLE library_selections (
 user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE
);
INSERT INTO library_selections(user_id,library_id) SELECT user_id,library_id FROM personal_libraries;
CREATE TRIGGER library_selection_access_insert BEFORE INSERT ON library_selections
WHEN NOT EXISTS(SELECT 1 FROM libraries l WHERE l.id=NEW.library_id AND l.state='active' AND (l.kind='personal' AND l.owner_user_id=NEW.user_id OR EXISTS(SELECT 1 FROM household_memberships m WHERE m.library_id=l.id AND m.user_id=NEW.user_id AND m.state='active')))
BEGIN SELECT RAISE(ABORT,'Library selection is not authorized'); END;
CREATE TRIGGER library_selection_access_update BEFORE UPDATE ON library_selections
WHEN NOT EXISTS(SELECT 1 FROM libraries l WHERE l.id=NEW.library_id AND l.state='active' AND (l.kind='personal' AND l.owner_user_id=NEW.user_id OR EXISTS(SELECT 1 FROM household_memberships m WHERE m.library_id=l.id AND m.user_id=NEW.user_id AND m.state='active')))
BEGIN SELECT RAISE(ABORT,'Library selection is not authorized'); END;

CREATE TRIGGER library_settings_identity_immutable BEFORE UPDATE OF library_id,integration,owner_user_id ON library_settings
WHEN NEW.library_id!=OLD.library_id OR NEW.integration!=OLD.integration OR NEW.owner_user_id!=OLD.owner_user_id
BEGIN SELECT RAISE(ABORT,'Shared connector identity is immutable'); END;

-- New accounts receive a private scope only; never an automatic household join.
CREATE TRIGGER household_new_account_personal_scope AFTER INSERT ON users
BEGIN
 INSERT INTO libraries(id,kind,owner_user_id,name) VALUES(lower(hex(randomblob(16))),'personal',NEW.id,'Personal library');
 INSERT INTO personal_libraries(user_id,library_id) SELECT NEW.id,id FROM libraries WHERE kind='personal' AND owner_user_id=NEW.id;
 INSERT INTO library_selections(user_id,library_id) SELECT NEW.id,library_id FROM personal_libraries WHERE user_id=NEW.id;
END;

-- One durable restriction per operational owner/provider, shared by every actor/job.
CREATE TABLE provider_cooldowns (
 owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 provider_kind TEXT NOT NULL CHECK(provider_kind IN ('realdebrid')),
 not_before TEXT,
 manual_review INTEGER NOT NULL DEFAULT 0 CHECK(manual_review IN (0,1)),
 updated_at TEXT NOT NULL,
 PRIMARY KEY(owner_user_id,provider_kind),
 CHECK(not_before IS NOT NULL OR manual_review=1)
);
CREATE INDEX library_request_due ON library_request_events(state,next_attempt_at,updated_at);
