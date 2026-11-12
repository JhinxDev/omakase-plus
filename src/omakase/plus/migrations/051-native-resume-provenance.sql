CREATE TABLE web_playback_native_resumes (
    target_key TEXT PRIMARY KEY REFERENCES web_playback_resumes(target_key) ON DELETE CASCADE,
    watch_barrier INTEGER NOT NULL CHECK(watch_barrier >= 0)
);

-- A writer that changes the playhead replaces the previous provenance.
-- A native checkpoint records its own provenance within that same transaction.
CREATE TRIGGER web_playback_resume_provenance_replaced
AFTER UPDATE OF position_seconds ON web_playback_resumes
BEGIN
    DELETE FROM web_playback_native_resumes WHERE target_key=NEW.target_key;
END;
