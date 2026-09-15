from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_project_env(project_root: Path | None = None) -> bool:
    """Load `<project_root>/.env` into os.environ if the file exists."""
    root = project_root or Path(__file__).resolve().parents[1]
    env_file = root / ".env"
    if not env_file.is_file():
        return False
    try:
        from dotenv import load_dotenv
    except ImportError:
        logger.warning(
            "python-dotenv not installed; .env file is ignored. "
            "Install with: pip install python-dotenv"
        )
        return False
    load_dotenv(env_file, override=False)
    logger.info("Loaded environment from %s", env_file)
    return True
