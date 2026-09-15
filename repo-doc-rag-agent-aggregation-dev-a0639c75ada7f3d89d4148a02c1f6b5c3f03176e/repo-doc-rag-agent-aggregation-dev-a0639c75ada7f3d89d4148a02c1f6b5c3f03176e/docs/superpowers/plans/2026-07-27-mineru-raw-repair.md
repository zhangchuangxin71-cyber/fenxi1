# MinerU Raw Repair Implementation Plan

1. Add tests for raw completeness and a database patch that changes only `raw.raw_mineru`.
2. Add submission tests for the narrowly scoped duplicate-repair eligibility rules.
3. Add runner tests proving repair bypasses indexing, LLM work, and full persistence.
4. Implement the storage, submission, and runner changes with the existing MinerU adapter.
5. Run focused and full ingestion tests, then perform a real repair through the standard API.
6. Restart ingestion and retrieval, verify `/raw`, commit, and push `dev`.
