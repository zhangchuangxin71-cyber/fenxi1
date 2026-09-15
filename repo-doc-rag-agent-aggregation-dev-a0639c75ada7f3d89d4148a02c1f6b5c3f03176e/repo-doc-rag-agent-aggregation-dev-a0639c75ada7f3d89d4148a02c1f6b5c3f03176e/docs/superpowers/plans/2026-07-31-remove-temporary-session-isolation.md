# Remove Temporary Document Session Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make temporary documents reusable and readable across sessions inside the same `user_id + kb_id` scope without changing `document_bindings`.

**Architecture:** Keep `session_id` as a backward-compatible request and task metadata field, but remove it from ingestion duplicate decisions and retrieval SQL authorization. A duplicate temporary upload reuses the existing binding and task result; permanent duplicate behavior and explicit deletion APIs remain unchanged.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, PostgreSQL, pytest.

---

### Task 1: Lock the temporary duplicate-ingestion behavior

**Files:**
- Modify: `src/repo-doc-ingestion/tests/test_mineru_raw_repair.py`
- Modify: `src/repo-doc-ingestion/app/ingestion/container.py`

- [ ] **Step 1: Write a failing service test**

Add a test that submits a temporary document whose `doc_id` already has a binding in the same user and knowledge base, with a different `session_id`. Configure the fake store with an existing completed task and assert that submission returns the existing `doc_id`, `status="completed"`, and `is_duplicate=True`, without queuing a task or starting workers.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_mineru_raw_repair.py -k temporary_duplicate -q
```

Expected: failure caused by the current `DuplicateDocumentError` branch.

- [ ] **Step 3: Implement the minimal duplicate branch**

In `IngestionService._submit_common`, when an existing binding is found and `is_temp=True`, clean the submit directory and return the existing scoped task result instead of entering repair, parsing, or duplicate-error paths. Keep permanent duplicate and MinerU repair behavior unchanged. If legacy/rebuilt data has a binding but no task row, persist one lightweight completed task so the required `task_id` remains queryable.

- [ ] **Step 4: Run the ingestion unit tests**

Run:

```bash
.venv/bin/pytest tests/test_mineru_raw_repair.py -q
```

Expected: all tests pass, including permanent duplicate and MinerU repair cases.

### Task 2: Remove session-based retrieval authorization

**Files:**
- Modify: `src/rag-retrieval-service/tests/contract/test_retrieve_schemas.py`
- Modify: `src/rag-retrieval-service/tests/unit/test_repository_sql.py`
- Modify: `src/rag-retrieval-service/app/api/schemas.py`
- Modify: `src/rag-retrieval-service/app/db/repositories.py`

- [ ] **Step 1: Rewrite contract and SQL tests for the new scope**

Assert that document metadata accepts `temp_doc_ids` without `session_id`. Assert that scope, metadata, page, and node SQL never contains a `b.session_id` predicate and remains constrained by `user_id`, `kb_id`, requested document IDs, binding type where applicable, and ready status.

- [ ] **Step 2: Run the focused retrieval tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/contract/test_retrieve_schemas.py tests/unit/test_repository_sql.py -q
```

Expected: failures from the current session requirement and SQL predicates.

- [ ] **Step 3: Implement knowledge-base-scoped visibility**

Remove the `DocumentMetaRequest` validator that requires a session for temporary IDs. In repository SQL builders, ignore `session_id` for authorization: explicit temporary IDs match temporary bindings in the same user/knowledge-base scope; page and node reads accept either binding type in that scope; an unbounded scope includes permanent and temporary bindings. Keep method signatures temporarily for caller compatibility.

- [ ] **Step 4: Run retrieval contract and repository tests**

Run:

```bash
.venv/bin/pytest tests/contract/test_retrieve_schemas.py tests/unit/test_repository_sql.py tests/unit/test_document_service.py -q
```

Expected: all tests pass.

### Task 3: Remove ingestion storage's optional session scope

**Files:**
- Modify: `src/repo-doc-ingestion/tests/test_storage_text_persistence.py`
- Modify: `src/repo-doc-ingestion/app/storage/postgres_store.py`

- [ ] **Step 1: Add a failing storage scope test**

Construct `PostgresWorkspaceStore` without initializing tables and assert `_scope_clause` contains only `user_id` and `kb_id`, even when a session and the legacy `scope_by_session=True` option are supplied.

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_storage_text_persistence.py -q
```

Expected: the scope currently includes `session_id`.

- [ ] **Step 3: Remove session filtering from `_scope_clause`**

Retain constructor arguments and stored session metadata for compatibility, but make workspace visibility depend only on `user_id + kb_id`.

- [ ] **Step 4: Run storage tests**

Run:

```bash
.venv/bin/pytest tests/test_storage_text_persistence.py tests/test_ingestion_db.py -q
```

Expected: unit tests pass; database integration tests may skip when PostgreSQL is unavailable.

### Task 4: Align public documentation and changelogs

**Files:**
- Modify: `src/repo-doc-ingestion/README.md`
- Modify: `src/repo-doc-ingestion/docs/api.md`
- Modify: `src/repo-doc-ingestion/docs/data_structure.md`
- Modify: `src/repo-doc-ingestion/CHANGELOG.md`
- Modify: `src/rag-retrieval-service/README.md`
- Modify: `src/rag-retrieval-service/docs/api.md`
- Modify: `src/rag-retrieval-service/docs/environment-variables.md`
- Modify: `src/rag-retrieval-service/CHANGELOG.md`

- [ ] **Step 1: Document the new boundary**

State consistently that `session_id` is accepted for compatibility and upstream bookkeeping, but document visibility and duplicate handling use `user_id + kb_id`. Document that repeated temporary uploads return the existing `doc_id` without creating another binding or rerunning ingestion.

- [ ] **Step 2: Preserve deletion documentation**

Keep explicit document/session deletion endpoints documented as caller-driven cleanup operations; do not claim the service performs session-based read isolation.

### Task 5: Full verification

**Files:**
- Verify only; no planned production edits.

- [ ] **Step 1: Run both project test suites**

Run:

```bash
src/repo-doc-ingestion/.venv/bin/pytest src/repo-doc-ingestion/tests -q
src/rag-retrieval-service/.venv/bin/pytest src/rag-retrieval-service/tests -q
```

Expected: all non-environmental tests pass; live/API tests may skip when their external dependencies are unavailable.

- [ ] **Step 2: Review the final diff**

Confirm no binding DDL, parsing workflow, permanent duplicate behavior, or deletion implementation changed. Confirm no retrieval SQL contains `b.session_id` and no request validator requires a session for temporary IDs.
