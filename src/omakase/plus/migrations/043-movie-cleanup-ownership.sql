-- Pre-existing submissions and imported copies have no cleanup ownership.
ALTER TABLE movie_submissions
    ADD COLUMN cleanup_owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE movie_submissions
    ADD COLUMN cleanup_tag TEXT CHECK (
        cleanup_tag IS NULL OR (
            length(cleanup_tag) = 32 AND cleanup_tag NOT GLOB '*[^0-9a-f]*'
        )
    );
