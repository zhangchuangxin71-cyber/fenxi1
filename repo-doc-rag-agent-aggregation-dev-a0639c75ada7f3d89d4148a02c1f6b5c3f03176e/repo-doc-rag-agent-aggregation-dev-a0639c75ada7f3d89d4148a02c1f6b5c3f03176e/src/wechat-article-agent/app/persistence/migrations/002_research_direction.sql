ALTER TABLE article_artifacts
    ADD COLUMN IF NOT EXISTS research_direction jsonb NULL;
