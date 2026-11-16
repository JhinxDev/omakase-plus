-- Per-profile Home row layout, shared by the web Home page and the Apple TV
-- client. One ordered list of shelf identifiers per viewer; hidden shelves keep
-- their position so Edit Home can add them back where they were.
CREATE TABLE viewer_home_layout (
 user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 rows_json TEXT NOT NULL DEFAULT '[]',
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
 updated_at TEXT NOT NULL
);
