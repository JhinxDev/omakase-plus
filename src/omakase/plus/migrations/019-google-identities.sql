-- Google is an optional login method for existing private accounts only.
CREATE TABLE IF NOT EXISTS google_identities (
    google_sub TEXT PRIMARY KEY CHECK (length(google_sub) BETWEEN 1 AND 255),
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS google_login_flows (
    state_hash TEXT PRIMARY KEY,
    browser_hash TEXT NOT NULL,
    nonce TEXT NOT NULL,
    code_verifier TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('login', 'link')),
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    session_hash TEXT NOT NULL DEFAULT '',
    expires_at INTEGER NOT NULL,
    CHECK ((purpose = 'login' AND user_id IS NULL AND session_hash = '')
        OR (purpose = 'link' AND user_id IS NOT NULL AND session_hash <> ''))
);
CREATE INDEX IF NOT EXISTS idx_google_flow_expiry ON google_login_flows(expires_at);
