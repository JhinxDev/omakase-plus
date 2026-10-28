ALTER TABLE media_candidate_attempts
ADD COLUMN source_title TEXT NOT NULL DEFAULT ''
CHECK (length(source_title) <= 300);
