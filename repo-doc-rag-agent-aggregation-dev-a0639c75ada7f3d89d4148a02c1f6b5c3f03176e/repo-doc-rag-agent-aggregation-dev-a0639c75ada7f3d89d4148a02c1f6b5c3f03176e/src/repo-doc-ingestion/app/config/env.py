from __future__ import annotations

"""Runtime environment bootstrap utilities."""

import os
from pathlib import Path


def _load_dotenv_file(repo_root: Path) -> None:
    """Load `.env` from the service root."""
    env_path = repo_root / ".env"
    if not env_path.is_file():
        return

    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(dotenv_path=env_path, override=False)
    except Exception:
        pass

    try:
        text = env_path.read_text(encoding="utf-8-sig")
    except Exception:
        return

    # Fill variables that are missing or empty. This keeps explicit process env
    # values higher priority while still fixing empty shell variables.
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if not str(os.environ.get(key, "") or "").strip():
            os.environ[key] = value


def _bridge_ark_to_openai() -> None:
    """Expose ARK credentials through OpenAI-compatible env names when absent."""
    ark_key = str(os.getenv("ARK_API_KEY", "") or "").strip()
    if ark_key and not str(os.getenv("OPENAI_API_KEY", "") or "").strip():
        os.environ["OPENAI_API_KEY"] = ark_key

    ark_base = str(os.getenv("ARK_BASE_URL", "") or "").strip()
    if ark_base and not str(os.getenv("OPENAI_BASE_URL", "") or "").strip():
        os.environ["OPENAI_BASE_URL"] = ark_base


def bootstrap_runtime_env(repo_root: Path) -> None:
    """Load environment variables and compatibility aliases."""
    _load_dotenv_file(repo_root)
    _bridge_ark_to_openai()

