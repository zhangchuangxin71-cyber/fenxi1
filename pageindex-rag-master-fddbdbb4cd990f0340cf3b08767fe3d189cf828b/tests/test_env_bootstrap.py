import os
from pathlib import Path

from core.env_bootstrap import bootstrap_runtime_env


def test_bootstrap_runtime_env_loads_dotenv_and_bridges_ark(monkeypatch, tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ARK_API_KEY=test-ark-key\n"
        "ARK_BASE_URL=https://ark.example.com/api/v3\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.delenv("ARK_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    bootstrap_runtime_env(tmp_path)

    assert os.environ["ARK_API_KEY"] == "test-ark-key"
    assert os.environ["OPENAI_API_KEY"] == "test-ark-key"
    assert os.environ["OPENAI_BASE_URL"] == "https://ark.example.com/api/v3"
