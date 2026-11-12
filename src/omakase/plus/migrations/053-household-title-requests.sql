ALTER TABLE household_memberships ADD COLUMN can_add_titles INTEGER CHECK(can_add_titles IN (0,1));
ALTER TABLE household_memberships ADD COLUMN can_request_titles INTEGER NOT NULL DEFAULT 1 CHECK(can_request_titles IN (0,1));

CREATE TABLE household_title_requests (
 id TEXT PRIMARY KEY,
 library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
 actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 domain TEXT NOT NULL CHECK(domain IN ('anime','movie','tv')),
 canonical_id TEXT NOT NULL,
 season_number INTEGER NOT NULL,
 title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 300),
 metadata_json TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','approved','declined','cancelled')),
 created_at TEXT NOT NULL,
 decided_at TEXT,
 CHECK((state='pending')=(decided_at IS NULL))
);
CREATE UNIQUE INDEX household_title_request_open_identity
 ON household_title_requests(library_id,actor_user_id,domain,canonical_id,season_number)
 WHERE state='pending';
CREATE INDEX household_title_request_review
 ON household_title_requests(library_id,state,created_at,id);
CREATE INDEX household_title_request_actor
 ON household_title_requests(library_id,actor_user_id,created_at,id);
