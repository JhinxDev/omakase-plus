-- Native Apple TV completions reuse the existing plex source slot for schema
-- compatibility and record the real source in a detail column. Existing
-- manual and Plex values remain unchanged and empty means the source is exact.
ALTER TABLE movie_history ADD COLUMN status_source_detail TEXT NOT NULL DEFAULT '';
ALTER TABLE movie_history ADD COLUMN rating_source_detail TEXT NOT NULL DEFAULT '';
ALTER TABLE tv_history ADD COLUMN status_source_detail TEXT NOT NULL DEFAULT '';
ALTER TABLE tv_history ADD COLUMN rating_source_detail TEXT NOT NULL DEFAULT '';
ALTER TABLE tv_episodes ADD COLUMN watch_source_detail TEXT NOT NULL DEFAULT '';
