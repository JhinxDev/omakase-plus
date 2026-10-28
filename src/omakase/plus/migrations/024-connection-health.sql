-- Health is a sanitized observation, separate from encrypted service credentials.
CREATE TABLE IF NOT EXISTS user_connection_health (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    integration TEXT NOT NULL CHECK (integration IN ('realdebrid','plex','riven','seerr','prowlarr')),
    fingerprint TEXT NOT NULL,
    health_json TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    PRIMARY KEY (user_id, integration)
);
