from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[4]
PROJECT = ROOT / "src" / "wechat-article-agent"


def test_compose_keeps_runtime_private_and_adapter_on_8140() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yaml").read_text(encoding="utf-8"))
    runtime = compose["services"]["wechat-article-runtime"]
    adapter = compose["services"]["wechat-article-agent"]
    database_init = compose["services"]["wechat-article-db-init"]

    assert runtime["expose"] == ["8141"]
    assert "ports" not in runtime
    assert runtime["build"]["dockerfile"] == "Runtime.Dockerfile"
    assert "LANGGRAPH_API_BASE_IMAGE" in runtime["build"]["args"]
    assert "PYPI_MIRROR_BASE" in runtime["build"]["args"]
    assert "REDIS_URI" not in runtime.get("environment", {})
    assert runtime["healthcheck"]
    assert adapter["ports"] == ["8140:8140"]
    assert adapter["environment"]["AGENT_SERVER_URL"] == "http://wechat-article-runtime:8141"
    assert adapter["environment"]["RETRIEVAL_BASE_URL"] == "http://rag-retrieval-service:8120"
    assert adapter["depends_on"]["wechat-article-runtime"]["condition"] == "service_healthy"
    assert database_init["command"] == ["python", "-m", "app.persistence.bootstrap"]
    assert database_init["restart"] == "no"
    assert "./env/wechat-article-db.env" in database_init["env_file"]
    assert runtime["depends_on"]["wechat-article-db-init"]["condition"] == "service_completed_successfully"
    assert adapter["depends_on"]["wechat-article-db-init"]["condition"] == "service_completed_successfully"
    assert "./env/wechat-article-db.env" not in runtime["env_file"]
    assert "./env/wechat-article-db.env" not in adapter["env_file"]


def test_images_run_as_non_root_and_exclude_development_payloads() -> None:
    adapter = (PROJECT / "Dockerfile").read_text(encoding="utf-8")
    runtime = (PROJECT / "Runtime.Dockerfile").read_text(encoding="utf-8")
    dockerignore = (PROJECT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert "USER app" in adapter
    assert "USER app" in runtime
    assert "0.12.3-py3.12-bookworm" in runtime
    assert "ARG LANGGRAPH_API_BASE_IMAGE=" in runtime
    assert "FROM ${LANGGRAPH_API_BASE_IMAGE}" in runtime
    assert "ENTRYPOINT []" in runtime
    assert 'CMD ["python", "-m", "langgraph_api.cli"' in runtime
    assert '--runtime-edition", "inmem' in runtime
    assert "REDIS_URI" not in runtime
    assert "--no-reload" in runtime
    assert {".env", "dev", "tests", "docs", ".langgraph_api"} <= set(dockerignore)


def test_runtime_config_uses_container_absolute_paths() -> None:
    runtime = (PROJECT / "Runtime.Dockerfile").read_text(encoding="utf-8")
    config = json.loads((PROJECT / "langgraph.runtime.json").read_text(encoding="utf-8"))

    assert "/deps/wechat-article-agent/langgraph.runtime.json" in runtime
    assert config["graphs"] == {
        "wechat_article": "/deps/wechat-article-agent/app/graph/builder.py:graph",
        "contract_probe": "/deps/wechat-article-agent/app/graph/contract_probe.py:graph",
    }
    assert config["checkpointer"]["path"] == (
        "/deps/wechat-article-agent/app/persistence/checkpointer.py:create_checkpointer"
    )
    assert not any(value.startswith("./") for value in config["graphs"].values())
    assert not config["checkpointer"]["path"].startswith("./")
    prefix = "/deps/wechat-article-agent/"
    specs = [*config["graphs"].values(), config["checkpointer"]["path"]]
    for spec in specs:
        container_path = spec.rsplit(":", 1)[0]
        assert container_path.startswith(prefix)
        assert (PROJECT / container_path.removeprefix(prefix)).is_file()


def test_deploy_script_includes_both_services_and_production_checks() -> None:
    deploy = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert "wechat-article-runtime" in deploy
    assert "wechat-article-agent" in deploy
    assert "require_value env/wechat-article.env DATABASE_URL" in deploy
    assert "require_value env/wechat-article.env ARK_API_KEY" in deploy
    assert "check_container_dsn env/wechat-article.env DATABASE_URL" in deploy
    assert "require_value env/wechat-article-db.env DATABASE_ADMIN_URL" in deploy
    assert "check_container_dsn env/wechat-article-db.env DATABASE_ADMIN_URL" in deploy
    assert "wechat-article-db-init" in deploy
    assert "DEBUG_ENABLED in env/wechat-article.env must be false" in deploy
