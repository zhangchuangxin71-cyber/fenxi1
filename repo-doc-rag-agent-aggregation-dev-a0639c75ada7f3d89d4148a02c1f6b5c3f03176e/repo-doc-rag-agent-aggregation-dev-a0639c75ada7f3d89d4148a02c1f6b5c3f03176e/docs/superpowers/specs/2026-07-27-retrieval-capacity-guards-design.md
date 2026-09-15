# Retrieval Capacity Guards Design

## Goal

Keep high-selectivity retrieval unchanged while bounding ambiguous document routing, active
`/retrieve` requests, queued requests, and idle PostgreSQL connections.

## Approved Behavior

- Preserve the existing keyword prefilter as stage one.
- Move keyword prefiltering into its own LangGraph node.
- If a group's stage-one candidates exceed 32, run deterministic group-specific identity-anchor
  extraction, scope IDF scoring, and stage-two filtering.
- If stage two still retains more than 32 documents, skip document-routing LLM work, build one
  token-bounded document-name metadata result with the approved downstream hint, and end the graph.
- Otherwise continue through the existing document-routing and retrieval nodes unchanged.
- Replace only the classification prompt's classification-standards section with the approved
  evidence-granularity definitions.
- Apply bounded fair admission only to `/retrieve`: 24 active, 36 queued, 120-second wait timeout.
- Reap PostgreSQL connections idle for 120 seconds every 30 seconds without shrinking below
  `DB_POOL_MIN`.

## Configuration

- `RAG_DOCUMENT_PREFILTER_MAX_CANDIDATES=32`
- `APP_MAX_CONCURRENCY=24`
- `APP_MAX_QUEUED_REQUESTS=36`
- `APP_ADMISSION_WAIT_TIMEOUT_SECONDS=120`
- `DB_POOL_IDLE_TTL_SECONDS=120`
- `DB_POOL_REAPER_INTERVAL_SECONDS=30`

## Safety Boundaries

- Overflow metadata content contains document names only and never exposes document IDs.
- The overflow hint is budgeted before names; names are appended only while the complete response
  remains within `max_return_tokens`.
- Health, readiness, document metadata, and raw-document endpoints bypass retrieval admission.
- Queued retrievals do not enter LangGraph or acquire database connections.
- Idle reclamation never closes checked-out connections and never reduces created connections below
  the configured pool minimum.
