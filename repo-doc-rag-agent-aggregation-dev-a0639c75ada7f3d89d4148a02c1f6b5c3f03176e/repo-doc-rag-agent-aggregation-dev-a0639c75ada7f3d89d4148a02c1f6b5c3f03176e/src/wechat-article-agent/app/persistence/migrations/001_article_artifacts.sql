CREATE TABLE IF NOT EXISTS article_artifacts (
    artifact_id text PRIMARY KEY,
    session_id text NOT NULL,
    run_id text NOT NULL,
    current_response_id text NOT NULL,
    active_runtime_run_id text NULL,
    revision integer NOT NULL CHECK (revision > 0),
    parent_artifact_id text NULL REFERENCES article_artifacts(artifact_id),
    status text NOT NULL CHECK (
        status IN ('running', 'waiting_for_input', 'cancelling', 'completed', 'failed', 'cancelled', 'superseded')
    ),
    current_stage text NOT NULL,
    approved_through_stage text NOT NULL DEFAULT 'none' CHECK (
        approved_through_stage IN ('none', 'docs_research', 'task_spec', 'outline', 'article')
    ),
    last_error jsonb NULL,
    doc_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    temp_doc_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    relevant_doc_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    document_coverage_mode text NOT NULL DEFAULT 'best_effort' CHECK (
        document_coverage_mode IN ('best_effort', 'all_required')
    ),
    material_library jsonb NULL,
    task_spec jsonb NULL,
    outline jsonb NULL,
    article_markdown text NULL,
    images jsonb NULL,
    final_html text NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    UNIQUE (session_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_article_artifacts_session_latest
    ON article_artifacts (session_id, revision DESC);
CREATE INDEX IF NOT EXISTS idx_article_artifacts_expiry
    ON article_artifacts (expires_at);
CREATE INDEX IF NOT EXISTS idx_article_artifacts_cancelling
    ON article_artifacts (updated_at)
    WHERE status = 'cancelling';
