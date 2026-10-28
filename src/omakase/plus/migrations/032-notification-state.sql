-- UI attention state only: completion, ratings and media ownership remain canonical elsewhere.
CREATE TABLE notification_state (
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 notification_id TEXT NOT NULL CHECK(length(notification_id) BETWEEN 1 AND 240),
 read_at TEXT,
 dismissed_at TEXT,
 PRIMARY KEY(user_id,notification_id),
 CHECK(read_at IS NOT NULL OR dismissed_at IS NOT NULL)
);

-- Header reads use bounded ordered windows, not a materialized all-history union.
CREATE INDEX idx_notification_anime_completed
 ON watch_entries(user_id,status,completed_at DESC,id DESC);
CREATE INDEX idx_notification_movie_completed
 ON movie_history(user_id,status,COALESCE(status_updated_at,updated_at) DESC,imdb_id);
CREATE INDEX idx_notification_tv_completed
 ON tv_history(user_id,status,updated_at DESC,tmdb_id);
CREATE INDEX idx_notification_anime_score
 ON watch_events(watch_entry_id,user_id,id DESC)
 WHERE disposition='accepted' AND score_ten IS NOT NULL;

-- Read-only shared-ownership checks must not scan global inventories per card.
CREATE INDEX idx_notification_provider_reference ON media_assets(provider_asset_id,monitor_id)
 WHERE status!='removed';
CREATE INDEX idx_notification_title_identity ON media_monitors(anilist_id,season_number,id);
CREATE INDEX idx_notification_removal_monitor ON media_title_removals(monitor_id,user_id,status,completed_at);
