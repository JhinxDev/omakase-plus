ALTER TABLE media_assets
ADD COLUMN requires_strict_inventory INTEGER NOT NULL DEFAULT 0
CHECK (requires_strict_inventory IN (0, 1));
