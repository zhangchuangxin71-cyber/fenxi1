# RAG Agent MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a runnable FastAPI + LangGraph RAG Agent MVP from `/opt/example-user/rag-agent.md`.

**Architecture:** The service exposes one SSE chat endpoint and runs a stateless LangGraph per request. LLM and RAG are behind small adapters so tests can mock them, while production can use Doubao Ark through the OpenAI-compatible SDK and a later replacement of `app/rag/stub.py`.

**Tech Stack:** Python 3.12-compatible code, FastAPI, sse-starlette, Pydantic v2, pydantic-settings, LangGraph, OpenAI SDK, psycopg, pytest, pytest-asyncio, httpx.

---

### Task 1: Project Skeleton And Config

**Files:**
- Create: `README.md`
- Create: `requirements.txt`
- Create: `.env.example`
- Create: `pyproject.toml`
- Create: `main.py`
- Create: `app/config.py`
- Create: package `__init__.py` files

- [ ] Create files exactly under `/opt/example-user/rag_agent_codex`.
- [ ] Add `Settings` with `.env` support and defaults from the product doc.
- [ ] Add FastAPI app factory and `/health`.

### Task 2: Contracts And Event Streaming

**Files:**
- Create: `app/api/schemas.py`
- Create: `app/api/sse.py`
- Create: `app/utils/ids.py`
- Create: `app/utils/safety.py`
- Create: `app/utils/retry.py`

- [ ] Implement request/outline schemas and `normalize_outline`.
- [ ] Implement `EventEmitter` with semantic event methods.
- [ ] Implement graph and LLM safety wrappers.

### Task 3: External Adapters

**Files:**
- Create: `app/llm/base.py`
- Create: `app/llm/types.py`
- Create: `app/llm/doubao.py`
- Create: `app/llm/registry.py`
- Create: `app/rag/types.py`
- Create: `app/rag/stub.py`
- Create: `app/db/documents.py`
- Create: `app/logging_ext/setup.py`
- Create: `app/logging_ext/oss_handler.py`

- [ ] Implement provider abstraction and Doubao Ark provider.
- [ ] Implement RAG stub contract.
- [ ] Implement document metadata loader with safe fallback.
- [ ] Implement local logging and optional OSS handler.

### Task 4: Agent Graph

**Files:**
- Create: `app/agent/state.py`
- Create: `app/agent/router.py`
- Create: `app/agent/graph.py`
- Create: `app/agent/prompts/*.py`
- Create: `app/agent/nodes/*.py`

- [ ] Implement intent routing rules and LLM fallback.
- [ ] Implement document routing, retrieval, QA, chitchat, outline generate/modify, report write/edit.
- [ ] Implement reference filtering and structured outline parsing fallbacks.

### Task 5: API Route

**Files:**
- Create: `app/api/routes.py`

- [ ] Build initial state from `ChatRequest`.
- [ ] Run graph in a background task.
- [ ] Stream SSE events and cancel runner when client disconnects.

### Task 6: Tests And MVP Verification

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/fixtures/mock_rag.py`
- Create: `tests/test_intent.py`
- Create: `tests/test_doc_router.py`
- Create: `tests/test_graph_qa.py`
- Create: `tests/test_graph_report.py`
- Create: `tests/test_api_sse.py`

- [ ] Write tests before finalizing production behavior.
- [ ] Mock LLM and RAG to verify the eight acceptance scenarios.
- [ ] Run `pytest`.
- [ ] Start the service and smoke-test `/health` plus the SSE endpoint.
