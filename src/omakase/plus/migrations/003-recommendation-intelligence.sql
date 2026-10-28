CREATE TABLE IF NOT EXISTS recommendation_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    media_id INTEGER,
    title TEXT NOT NULL,
    feedback_type TEXT NOT NULL CHECK (feedback_type IN ('interested', 'not_for_me', 'wrong_sequel', 'already_watched')),
    run_id INTEGER REFERENCES run_history(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_recommendation_feedback_user_created
ON recommendation_feedback(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_recommendation_feedback_user_media
ON recommendation_feedback(user_id, media_id);

ALTER TABLE run_history ADD COLUMN lane TEXT NOT NULL DEFAULT 'best_match';
