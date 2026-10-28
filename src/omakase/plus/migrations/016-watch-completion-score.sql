ALTER TABLE watch_events ADD COLUMN score_ten INTEGER
    CHECK (score_ten IS NULL OR (typeof(score_ten) = 'integer' AND score_ten BETWEEN 1 AND 10
        AND source = 'manual' AND kind = 'completed'));
ALTER TABLE tracker_sync_outbox ADD COLUMN score_ten INTEGER
    CHECK (score_ten IS NULL OR (typeof(score_ten) = 'integer' AND score_ten BETWEEN 1 AND 10
        AND action = 'complete'));
