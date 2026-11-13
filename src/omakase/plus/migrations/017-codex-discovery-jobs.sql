CREATE TABLE IF NOT EXISTS codex_workers (
    id TEXT PRIMARY KEY,
    heartbeat_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS codex_discovery_jobs (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK(state IN ('queued','running','completed','failed','expired')),
    model TEXT NOT NULL,
    effort TEXT NOT NULL,
    prompt TEXT NOT NULL DEFAULT '',
    result TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    deadline_at REAL NOT NULL,
    worker_id TEXT,
    lease_token TEXT,
    lease_expires_at REAL
);
CREATE INDEX IF NOT EXISTS idx_codex_jobs_state ON codex_discovery_jobs(state, created_at);
CREATE INDEX IF NOT EXISTS idx_codex_jobs_user ON codex_discovery_jobs(user_id, created_at);
