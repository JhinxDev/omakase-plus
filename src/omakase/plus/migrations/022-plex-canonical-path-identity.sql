ALTER TABLE plex_library_items ADD COLUMN canonical_title TEXT
    CHECK (canonical_title IS NULL OR length(canonical_title) BETWEEN 1 AND 300);
ALTER TABLE plex_library_items ADD COLUMN managed_paths_json TEXT
    CHECK (managed_paths_json IS NULL OR json_valid(managed_paths_json));
ALTER TABLE plex_library_items ADD COLUMN path_identity_complete INTEGER
    CHECK (path_identity_complete IS NULL OR path_identity_complete IN (0, 1))
    CHECK ((managed_paths_json IS NULL) = (path_identity_complete IS NULL));
