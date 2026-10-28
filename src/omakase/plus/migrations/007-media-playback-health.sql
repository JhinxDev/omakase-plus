ALTER TABLE media_assets
ADD COLUMN unusable_coverage_json TEXT NOT NULL DEFAULT '[]'
CHECK (json_valid(unusable_coverage_json) = 1);
