# RAG Platform Unified Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone source-based repository that deploys MinerU CPU, ingestion, retrieval, knowledge chat, and report agent with one Docker Compose command.

**Architecture:** Snapshot the five existing repositories without development/runtime artifacts. A single Compose file builds each child Dockerfile, injects one env file per service, joins one bridge network, and maps equal host/container ports. PostgreSQL remains external.

**Tech Stack:** Docker Compose, Bash, FastAPI OpenAPI 3.1, Python/PyYAML.

---

### Task 1: Snapshot service sources

**Files:**
- Create: `src/MinerU/**`
- Create: `src/repo-doc-ingestion/**`
- Create: `src/rag-retrieval-service/**`
- Create: `src/rag-knowledge-chat/**`
- Create: `src/rag-report-agent/**`

- [ ] Copy each current source tree while excluding `.git`, `.env`, `.venv`, caches, logs and generated outputs.
- [ ] Assert every expected Dockerfile and dependency manifest exists.
- [ ] Scan the snapshot for tracked real API key patterns.

### Task 2: Add configuration contract

**Files:**
- Create: `env/mineru.env.example`
- Create: `env/ingestion.env.example`
- Create: `env/retrieval.env.example`
- Create: `env/knowledge-chat.env.example`
- Create: `env/report-agent.env.example`
- Create: `docs/environment-variables.md`
- Create: `.gitignore`

- [ ] Derive each template from the child service Settings and production template.
- [ ] Replace host-local URLs with Compose service names.
- [ ] Keep PostgreSQL DSNs as explicit production placeholders.
- [ ] Document required, optional and capacity-sensitive variables.
- [ ] Verify every template variable appears in the documentation.

### Task 3: Add unified Compose

**Files:**
- Create: `docker-compose.yaml`

- [ ] Define exactly five build-based services and no database.
- [ ] Set CPU MinerU command, volumes and healthcheck.
- [ ] Set equal host/container port mappings 8135, 8100, 8115, 8120 and 8130.
- [ ] Attach every service to `rag-platform-network`.
- [ ] Add health-based dependencies without using fixed sleeps.
- [ ] Parse Compose as YAML and assert service names, build contexts, env files, ports and network membership.

### Task 4: Add deploy entrypoint

**Files:**
- Create: `scripts/deploy.sh`
- Create: `tests/test_deployment_contract.py`

- [ ] Write failing tests for required env files, Compose shape and deploy commands.
- [ ] Implement env preflight without printing values.
- [ ] Run `docker compose config --quiet`, build/up and bounded health polling.
- [ ] Add actionable failure logs and endpoint summary.
- [ ] Run `bash -n scripts/deploy.sh` and deployment contract tests.

### Task 5: Add operator documentation

**Files:**
- Create: `README.md`
- Create: `docs/deployment.md`
- Create: `.dockerignore`

- [ ] Document host prerequisites, env preparation, external PostgreSQL and CPU expectations.
- [ ] State that child Compose/deploy scripts are retained but not used for platform deployment.
- [ ] Document port exposure and firewall/authentication risk.
- [ ] Document build, start, status, logs, update and rollback commands.

### Task 6: Archive OpenAPI

**Files:**
- Create: `scripts/export_openapi.py`
