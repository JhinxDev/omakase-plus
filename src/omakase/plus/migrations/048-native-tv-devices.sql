CREATE TABLE native_tv_devices (
    token_digest TEXT PRIMARY KEY CHECK(length(token_digest)=64),
    code_digest TEXT UNIQUE CHECK(code_digest IS NULL OR length(code_digest)=64),
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    browser_session TEXT REFERENCES sessions(id) ON DELETE CASCADE,
    library_id TEXT REFERENCES libraries(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL,
    CHECK (
        (code_digest IS NOT NULL AND user_id IS NULL AND browser_session IS NULL AND library_id IS NULL)
        OR (code_digest IS NULL AND user_id IS NOT NULL AND browser_session IS NOT NULL AND library_id IS NOT NULL)
    )
);
CREATE INDEX native_tv_devices_owner ON native_tv_devices(user_id, expires_at);
