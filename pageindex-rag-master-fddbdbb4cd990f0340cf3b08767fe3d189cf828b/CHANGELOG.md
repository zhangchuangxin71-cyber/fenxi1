# Changelog

## Unreleased

- reorganized the repository into the `repo-agent-qa` layout with all business code under `app/`
- moved the PageIndex package and assistant runtime modules into `app/` without removing QA features
- kept PostgreSQL as the persistent storage backend and updated the main entrypoint to `app/document_assistant_agent.py`
- added `.env.example`, `Dockerfile`, and lightweight `pytest` coverage
- refreshed `README.md` and `.gitignore` for the new project layout
