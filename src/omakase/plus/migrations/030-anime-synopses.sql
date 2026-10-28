-- Public AniList synopsis cache; no owner watch, token or playback data.
CREATE TABLE IF NOT EXISTS anime_synopses (
    anilist_id INTEGER PRIMARY KEY CHECK (anilist_id > 0),
    description TEXT NOT NULL DEFAULT '',
    retry_at REAL NOT NULL
);
