# On-Demand MinerU Raw Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let `POST /rag/v1/documents/raw` asynchronously trigger an in-place MinerU repair and expose a read-only GET status API.

**Architecture:** Retrieval remains the public API and calls one internal ingestion repair endpoint. Ingestion reuses the existing MinerU-only runner and task queue, storing state under `raw.raw_mineru_repair`. No new table, queue, LLM call, or reindexing path is introduced.

**Tech Stack:** FastAPI, Pydantic v2, httpx, PostgreSQL JSONB, pytest.

---

### Task 1: Repair state storage

**Files:** `app/storage/postgres_store.py`, `app/ingestion/container.py`, storage repair tests.

- [ ] Add failing tests for state reads, atomic processing claims, lease expiry, terminal failures, transient cooldown, and writes limited to the two raw keys.
- [ ] Implement compact state helpers without changing document timestamps or normalized tables.
- [ ] Run focused tests to green.

### Task 2: Internal ingestion repair API

**Files:** ingestion container and MinerU repair tests.

- [ ] Add failing tests for scope validation, missing OSS key, completed raw, active-task reuse, permanent failure suppression, cooldown, and stable doc ID.
- [ ] Add an internal POST repair endpoint that resolves the stored OSS key and reuses the existing task queue and MinerU-only runner.
- [ ] Record processing/completed/classified-failed state while bypassing indexing, LLMs, descriptions, pages, nodes, and bindings.
- [ ] Run focused ingestion tests to green.

### Task 3: Retrieval trigger and status API

**Files:** retrieval settings/container/document service/repository/schemas/app, a small repair HTTP client, and document API tests.

- [ ] Add failing tests proving complete raw remains 200, missing raw returns 202 after one trigger, terminal failures return non-retryable 422, and GET polling is read-only.
- [ ] Add an injected async ingestion client with a short timeout and no retries.
- [ ] Add repair state to repository reads, preserve the existing 200 model, document 202, and add `GET /rag/v1/documents/raw/status`.
- [ ] Map missing OSS source to an error that instructs deletion and re-ingestion.
- [ ] Run focused retrieval tests to green.

### Task 4: Documentation and configuration

**Files:** `src/rag-retrieval-service/docs/api.md`, retrieval env examples, aggregate README only if needed.

- [ ] Document 200/202/status polling/error flows and delete/re-ingest recovery.
- [ ] Add only ingestion base URL and submission timeout configuration.
- [ ] Verify examples against OpenAPI.

### Task 5: Verification and delivery

- [ ] Run complete ingestion/retrieval tests, compile checks, and `git diff --check`.
- [ ] Restart ingestion and retrieval from aggregate env files.
- [ ] Live-test complete, missing-source, accepted, status, and completed raw flows; snapshot unchanged document fields and normalized tables.
- [ ] Commit, push `dev`, and verify the remote SHA.

### Task 6: Unify duplicate-ingestion repair state

**Files:** `src/repo-doc-ingestion/tests/test_mineru_raw_repair.py`, `src/repo-doc-ingestion/app/ingestion/container.py`.

- [ ] Add a failing test proving duplicate ingestion atomically claims a repair ID and persists its task ID.
- [ ] Reuse `claim_raw_mineru_repair` before queueing the existing MinerU-only runner path.
- [ ] Verify duplicate repair success and failure use the same ownership-checked marker updates as `/raw` repair.

### Task 7: Record initial-ingestion MinerU fallback

**Files:** `src/repo-doc-ingestion/tests/test_mineru_adapter.py`, `src/repo-doc-ingestion/app/parser/multiformat_parser.py`, `src/repo-doc-ingestion/docs/api.md`.

- [ ] Add a failing test proving MinerU-to-native fallback persists a failed `raw_mineru_repair` marker.
- [ ] Attach the marker only after MinerU fails and native fallback succeeds; leave normal initial ingestion unchanged.
- [ ] Document marker semantics, run full tests, restart ingestion/retrieval, commit, and push `dev`.
