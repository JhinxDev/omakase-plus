CREATE TABLE agent_audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 operation TEXT NOT NULL CHECK(operation IN ('preferences','monitor_active')),
 target TEXT NOT NULL,
 changed_fields_json TEXT NOT NULL CHECK(json_valid(changed_fields_json)),
 created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_agent_audit_user_id ON agent_audit(user_id,id);
