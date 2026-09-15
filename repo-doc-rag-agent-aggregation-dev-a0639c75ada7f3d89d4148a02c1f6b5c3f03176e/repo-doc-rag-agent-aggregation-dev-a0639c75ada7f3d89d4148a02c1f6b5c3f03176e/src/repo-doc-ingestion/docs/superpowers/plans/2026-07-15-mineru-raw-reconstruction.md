# MinerU Raw Reconstruction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist the necessary MinerU structured outputs and expose an authenticated retrieval-service endpoint that reconstructs one scoped document from normalized database tables.

**Architecture:** The ingestion adapter keeps MinerU's `md_content`, `content_list`, and `middle_json` under `documents.raw.raw_mineru`; normalized pages and node text remain only in `doc_pages` and `doc_nodes`. The retrieval service validates `user_id`, `kb_id`, and `doc_id`, loads the scoped document and its normalized children, rebuilds the existing PageIndex-shaped document, and returns it through `POST /rag/v1/documents/raw`.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, psycopg 3, PostgreSQL JSONB, pytest.

---

### Task 1: Preserve the MinerU Markdown artifact

**Files:**
- Modify: `app/parser/mineru_adapter.py`
- Modify: `tests/test_mineru_adapter.py`
- Modify: `tests/test_storage_text_persistence.py`

- [ ] Add assertions that `raw_mineru` contains `md_content`, `content_list`, and `middle_json`, while the storage slimmer removes normalized `pages` and nested node `text` but preserves `raw_mineru`.
- [ ] Run the focused tests and verify they fail because `md_content` is absent.
- [ ] Add `md_content` to `raw_mineru` without enabling `model_output` or image returns.
- [ ] Re-run the focused tests and the ingestion test suite.

### Task 2: Define and reconstruct the raw-document response

**Files:**
- Modify: `rag-retrieval-service/app/api/schemas.py`
- Modify: `rag-retrieval-service/app/db/documents.py`
- Modify: `rag-retrieval-service/app/retrieval/service.py`
- Modify: `rag-retrieval-service/tests/test_service_edges.py`
- Create: `rag-retrieval-service/tests/test_document_raw.py`

- [ ] Add failing schema/service tests for a single `doc_id`, reconstructed pages and nested node order, missing scope, and absent `raw_mineru`.
- [ ] Run the focused tests and verify failure because the request/response models and service method do not exist.
- [ ] Add strict request and response models. The response directly exposes the reconstructed PageIndex-shaped fields and identifies the contract as schema version `1.0`.
- [ ] Add a database helper that scopes the document through `document_bindings`, then reads ordered pages and nodes using the same pooled connection.
- [ ] Add a service method that returns `DOCUMENT_NOT_FOUND` when the scoped binding is unavailable and `DOCUMENT_RAW_NOT_AVAILABLE` when MinerU artifacts are absent.
- [ ] Re-run focused and full retrieval-service tests.

### Task 3: Add the authenticated endpoint and consumer documentation

**Files:**
- Modify: `rag-retrieval-service/app/api/routes.py`
- Modify: `rag-retrieval-service/tests/test_api_edges.py`
- Modify: `rag-retrieval-service/README.md`
- Create: `rag-retrieval-service/docs/document-raw-schema.md`
- Modify: `repo-doc-ingestion/README.md`
- Modify: `repo-doc-ingestion/CHANGELOG.md`
- Modify: `rag-retrieval-service/CHANGELOG.md`

- [ ] Add a failing API test proving authentication runs before rate limiting and the route delegates one scoped request.
- [ ] Implement `POST /rag/v1/documents/raw` through the existing authentication, user rate limiter, request timeout, request ID, and logging helpers.
- [ ] Document every request/response field, errors, MinerU artifact semantics, legacy-document behavior, and the deliberate exclusion of `model_output` and image binaries.
- [ ] Run formatting/static checks available in each repository and all automated tests; inspect both git diffs for unrelated changes.
