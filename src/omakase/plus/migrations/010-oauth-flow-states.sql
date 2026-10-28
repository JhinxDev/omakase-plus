CREATE TABLE IF NOT EXISTS oauth_flow_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider IN ('anilist')),
    state_hash TEXT NOT NULL CHECK (length(state_hash) = 64),
    code_verifier TEXT NOT NULL CHECK (length(code_verifier) BETWEEN 43 AND 256),
    expires_at INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(provider, state_hash)
);

CREATE INDEX IF NOT EXISTS idx_oauth_flow_states_owner
    ON oauth_flow_states(user_id, provider, expires_at);
