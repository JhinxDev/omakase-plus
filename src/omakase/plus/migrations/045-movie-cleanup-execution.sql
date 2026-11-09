ALTER TABLE movie_cleanup_jobs
ADD COLUMN stage TEXT NOT NULL DEFAULT 'prepare'
CHECK (stage IN ('prepare', 'withdrawal_intent', 'awaiting_plex', 'provider_intent', 'complete', 'blocked'));

ALTER TABLE movie_cleanup_jobs
ADD COLUMN proof_json TEXT CHECK (proof_json IS NULL OR (json_valid(proof_json) AND length(proof_json) <= 4096));

ALTER TABLE movie_cleanup_jobs
ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 12);

ALTER TABLE movie_cleanup_jobs ADD COLUMN retry_at TEXT;
ALTER TABLE movie_cleanup_jobs ADD COLUMN reason TEXT;

CREATE INDEX movie_cleanup_due ON movie_cleanup_jobs (retry_at, queued_at)
WHERE state='queued' AND stage NOT IN ('complete', 'blocked');
