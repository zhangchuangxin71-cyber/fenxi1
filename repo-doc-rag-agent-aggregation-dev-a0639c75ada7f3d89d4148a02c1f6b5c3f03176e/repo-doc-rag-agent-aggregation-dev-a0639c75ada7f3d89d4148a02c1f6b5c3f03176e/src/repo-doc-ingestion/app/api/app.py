from __future__ import annotations

"""FastAPI application entrypoint for repo-doc-ingestion.

"""

import logging
import os
from typing import Any


from app.ingestion.container import create_app  # noqa: E402



def _setup_logging() -> None:
    level_name = str(os.getenv("LOG_LEVEL", "INFO") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        )
    root.setLevel(level)
    logging.getLogger("app").setLevel(level)


_setup_logging()
logger = logging.getLogger(__name__)


app: Any = create_app()


@app.get("/")
async def root() -> dict[str, str]:
    """Service metadata endpoint."""
    return {
        "service": "repo-doc-ingestion",
        "status": "ok",
        "docs": "/docs",
        "health": "/healthz",
    }
