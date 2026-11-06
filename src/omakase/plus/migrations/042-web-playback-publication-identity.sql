-- Durable, internal resume provenance. Legacy rows stay unknown, never guessed
-- from expired browser sessions. Resume positions/history/cycles remain intact.
ALTER TABLE web_playback_resumes ADD COLUMN publication_identity TEXT
 CHECK(publication_identity IS NULL OR
       (typeof(publication_identity)='text' AND length(publication_identity)=64
        AND publication_identity NOT GLOB '*[^0-9a-f]*'));
