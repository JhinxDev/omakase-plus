-- First observed user-facing add, not a catalog refresh or provider request.
CREATE TABLE tv_collection_additions (
    user_id INTEGER NOT NULL,
    tmdb_id INTEGER NOT NULL,
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 300),
    source TEXT NOT NULL CHECK(source IN ('watch_status_save','episode_watch_save')),
    added_at TEXT NOT NULL,
    PRIMARY KEY(user_id,tmdb_id),
    FOREIGN KEY(user_id,tmdb_id) REFERENCES tv_catalog(user_id,tmdb_id) ON DELETE CASCADE
);
CREATE INDEX tv_collection_additions_recent
    ON tv_collection_additions(user_id,added_at DESC,tmdb_id DESC);
