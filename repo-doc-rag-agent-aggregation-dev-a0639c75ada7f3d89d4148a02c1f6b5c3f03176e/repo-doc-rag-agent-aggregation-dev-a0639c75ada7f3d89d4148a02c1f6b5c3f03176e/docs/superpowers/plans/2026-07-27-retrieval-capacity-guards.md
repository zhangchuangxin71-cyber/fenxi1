# Retrieval Capacity Guards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bound ambiguous document routing and process resources without changing successful retrieval behavior.

**Architecture:** A dedicated keyword LangGraph node produces either bounded prefilter results for the existing router or a complete budgeted overflow response that routes directly to `END`. A process-wide fair admission controller protects only `/retrieve`, while the existing PostgreSQL pool gains lifecycle-managed idle reclamation.

**Tech Stack:** Python 3.12, FastAPI, LangGraph, psycopg 3, Pydantic, pytest/pytest-asyncio.

---

### Task 1: Configuration and classification contract

**Files:**
- Modify: `src/rag-retrieval-service/app/config/settings.py`
- Modify: `src/rag-retrieval-service/.env.example`
- Modify: `src/rag-retrieval-service/.env`
- Modify: `src/rag-retrieval-service/app/workflows/classification/strategies.py`
- Test: `src/rag-retrieval-service/tests/unit/test_settings.py`
- Test: `src/rag-retrieval-service/tests/unit/test_classification_strategies.py`

- [ ] Add failing settings tests for the five approved capacity settings and their validation.
- [ ] Run the focused tests and confirm failure because fields are absent.
- [ ] Add settings, environment examples, and the approved classification-standards text.
- [ ] Run focused tests and confirm they pass.

### Task 2: Two-stage keyword node and overflow response

**Files:**
- Create: `src/rag-retrieval-service/app/workflows/document_routing/prefilter.py`
- Modify: `src/rag-retrieval-service/app/workflows/document_routing/models.py`
- Modify: `src/rag-retrieval-service/app/workflows/document_routing/service.py`
- Modify: `src/rag-retrieval-service/app/graph/state.py`
- Modify: `src/rag-retrieval-service/app/graph/builder.py`
- Modify: `src/rag-retrieval-service/app/container.py`
- Test: `src/rag-retrieval-service/tests/unit/test_document_routing.py`
- Test: `src/rag-retrieval-service/tests/unit/test_graph.py`

- [ ] Add failing tests proving stage one is unchanged, discriminative anchors reduce homogeneous candidates, ambiguous candidates overflow, metadata omits IDs, and content stays within budget.
- [ ] Run focused tests and confirm expected failures.
- [ ] Implement deterministic anchors, scope IDF, secondary filtering, overflow result construction, and router consumption of precomputed results.
- [ ] Add the keyword node and conditional `END` edge while retaining the normal downstream graph path.
- [ ] Run focused tests and confirm they pass.

### Task 3: Fair `/retrieve` admission

**Files:**
- Create: `src/rag-retrieval-service/app/security/admission.py`
- Modify: `src/rag-retrieval-service/app/container.py`
- Modify: `src/rag-retrieval-service/app/api/app.py`
- Test: `src/rag-retrieval-service/tests/unit/test_admission.py`
- Test: `src/rag-retrieval-service/tests/integration/test_api.py`

- [ ] Add failing tests for immediate admission, bounded queue rejection, timeout/cancellation cleanup, user round-robin, and endpoint scope.
- [ ] Run focused tests and confirm expected failures.
- [ ] Implement the singleton admission controller and wrap only `/retrieve`.
- [ ] Run focused tests and confirm they pass.

### Task 4: Idle PostgreSQL connection reclamation

**Files:**
- Modify: `src/rag-retrieval-service/app/db/pool.py`
- Modify: `src/rag-retrieval-service/app/container.py`
- Test: `src/rag-retrieval-service/tests/unit/test_pool.py`

- [ ] Add failing tests for TTL reclamation, minimum retention, checked-out safety, and shutdown.
- [ ] Run pool tests and confirm expected failures.
- [ ] Add timestamped idle entries and a lifecycle-managed reaper.
- [ ] Run pool tests and confirm they pass.

### Task 5: Full verification and documentation consistency

**Files:**
- Modify only if required by verification: retrieval-service README/API/implementation docs.

- [ ] Run `uv run pytest tests/unit tests/contract tests/integration -q`.
- [ ] Run `uv run ruff check app tests`.
- [ ] Inspect the final diff for unrelated changes and configuration drift.
- [ ] Verify `.env.example` uses the approved production-oriented defaults.
