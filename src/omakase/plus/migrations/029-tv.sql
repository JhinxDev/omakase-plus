CREATE TABLE tv_catalog (
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 tmdb_id INTEGER NOT NULL CHECK(tmdb_id > 0), metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)), updated_at TEXT NOT NULL,
 PRIMARY KEY(user_id,tmdb_id));
CREATE TABLE tv_history (
 user_id INTEGER NOT NULL, tmdb_id INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'unwatched' CHECK(status IN ('unwatched','watching','completed','dropped')),
 rating REAL CHECK(rating IS NULL OR (rating BETWEEN 1 AND 10 AND rating*2=CAST(rating*2 AS INTEGER))),
 status_source TEXT NOT NULL DEFAULT 'none' CHECK(status_source IN ('none','manual','plex')),
 rating_source TEXT NOT NULL DEFAULT 'none' CHECK(rating_source IN ('none','manual','plex')),
 updated_at TEXT NOT NULL, PRIMARY KEY(user_id,tmdb_id),
 FOREIGN KEY(user_id,tmdb_id) REFERENCES tv_catalog(user_id,tmdb_id) ON DELETE CASCADE);
CREATE TABLE tv_episodes (
 user_id INTEGER NOT NULL, tmdb_id INTEGER NOT NULL,
 season_number INTEGER NOT NULL CHECK(season_number BETWEEN 0 AND 200), episode_number INTEGER NOT NULL CHECK(episode_number BETWEEN 1 AND 1000),
 metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)), watched INTEGER NOT NULL DEFAULT 0 CHECK(watched IN (0,1)),
 watch_source TEXT NOT NULL DEFAULT 'none' CHECK(watch_source IN ('none','manual','plex')),
 plex_present INTEGER NOT NULL DEFAULT 0 CHECK(plex_present IN (0,1)), updated_at TEXT NOT NULL,
 PRIMARY KEY(user_id,tmdb_id,season_number,episode_number),
 FOREIGN KEY(user_id,tmdb_id) REFERENCES tv_catalog(user_id,tmdb_id) ON DELETE CASCADE);
CREATE TABLE tv_plex_snapshots (
 user_id INTEGER NOT NULL, tmdb_id INTEGER NOT NULL, source_scope TEXT NOT NULL, plex_key TEXT NOT NULL,
 complete INTEGER NOT NULL CHECK(complete IN (0,1)), observed_at TEXT NOT NULL,
 PRIMARY KEY(user_id,tmdb_id), FOREIGN KEY(user_id,tmdb_id) REFERENCES tv_catalog(user_id,tmdb_id) ON DELETE CASCADE);
