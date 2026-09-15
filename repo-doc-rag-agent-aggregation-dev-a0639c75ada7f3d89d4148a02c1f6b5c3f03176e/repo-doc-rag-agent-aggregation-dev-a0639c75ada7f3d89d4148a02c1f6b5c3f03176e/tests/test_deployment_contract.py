from __future__ import annotations

import json
import re
import runpy
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "mineru-api": {
        "context": "./src/MinerU",
        "dockerfile": "Dockerfile",
        "env": "./env/mineru.env",
        "port": 8135,
    },
    "repo-doc-ingestion": {
        "context": "./src/repo-doc-ingestion",
        "dockerfile": "Dockerfile",
        "env": "./env/ingestion.env",
        "port": 8100,
    },
    "rag-report-agent": {
        "context": "./src/rag-report-agent",
        "dockerfile": "Dockerfile",
        "env": "./env/report-agent.env",
        "port": 8115,
    },
    "rag-retrieval-service": {
        "context": "./src/rag-retrieval-service",
        "dockerfile": "Dockerfile",
        "env": "./env/retrieval.env",
        "port": 8120,
    },
    "rag-knowledge-chat": {
        "context": "./src/rag-knowledge-chat",
        "dockerfile": "Dockerfile",
        "env": "./env/knowledge-chat.env",
        "port": 8130,
    },
}


def _compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yaml").read_text(encoding="utf-8"))


def test_compose_defines_expected_platform_services() -> None:
    services = _compose()["services"]
    assert set(services) == set(EXPECTED) | {
        "wechat-article-db-init",
        "wechat-article-runtime",
        "wechat-article-agent",
    }
    assert "postgres" not in services


def test_each_service_uses_expected_source_env_port_and_network() -> None:
    services = _compose()["services"]
    for name, expected in EXPECTED.items():
        service = services[name]
        assert service["build"]["context"] == expected["context"]
        assert service["build"]["dockerfile"] == expected["dockerfile"]
        assert service["env_file"] == [expected["env"]]
        assert service["ports"] == [f"{expected['port']}:{expected['port']}"]
        if name == "mineru-api":
            assert service["networks"] == ["rag-platform-network"]
        else:
            assert service["networks"] == ["rag-platform-network", "pageindex"]
        assert service["restart"] == "unless-stopped"


def test_each_service_rotates_docker_json_logs() -> None:
    services = _compose()["services"]
    expected_logging = {
        "driver": "json-file",
        "options": {"max-size": "50m", "max-file": "3"},
    }
    for name, service in services.items():
        assert service["logging"] == expected_logging, name


def test_persistent_data_uses_host_bind_mounts_under_mnt_data() -> None:
    compose = _compose()
    services = compose["services"]
    assert services["mineru-api"]["volumes"] == [
        "/mnt/data/mineru/models:/data/models",
        "/mnt/data/mineru/output:/data/output",
    ]
    assert services["mineru-api"]["environment"] == {
        "HF_HOME": "/data/models/huggingface",
        "MINERU_TOOLS_CONFIG_JSON": "/data/models/mineru.json",
        "MODELSCOPE_CACHE": "/data/models/modelscope",
    }
    assert services["repo-doc-ingestion"]["volumes"] == [
        "/mnt/data/ingestion/workspace:/app/app/workspace",
        "/mnt/data/logs/ingestion:/app/logs",
    ]
    assert services["rag-report-agent"]["volumes"] == [
        "/mnt/data/logs/report-agent:/app/logs"
    ]
    assert compose["volumes"] == {"wechat-article-runtime-data": None}


def test_deploy_script_prepares_host_data_directories() -> None:
    script = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    for path in (
        "/mnt/data/mineru/models",
        "/mnt/data/mineru/output",
        "/mnt/data/ingestion/workspace",
        "/mnt/data/logs/ingestion",
        "/mnt/data/logs/report-agent",
    ):
        assert path in script
    assert "mkdir -p" in script
    assert "require_value env/knowledge-chat.env RAG_API_KEYS" not in script


def test_deploy_does_not_require_removed_retrieval_api_key_contract() -> None:
    script = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    examples = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "env/retrieval.env.example",
            "env/knowledge-chat.env.example",
            "env/report-agent.env.example",
        )
    )
    platform_docs = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in ("docs/environment-variables.md", "README.md", "CHANGELOG.md")
    )

    assert "check_key_contract" not in script
    assert "RAG_SERVICE_API_KEYS" not in script
    assert "RAG_RETRIEVAL_SERVICE_API_KEY" not in script
    assert "RAG_SERVICE_API_KEYS" not in examples
    assert "RAG_RETRIEVAL_SERVICE_API_KEY" not in examples
    assert "RAG_SERVICE_API_KEYS" not in platform_docs


def test_deploy_conditionally_validates_knowledge_chat_inbound_api_keys() -> None:
    script = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    chat = (ROOT / "env/knowledge-chat.env.example").read_text(encoding="utf-8")

    assert "check_knowledge_chat_inbound_auth" in script
    assert "KNOWLEDGE_CHAT_INBOUND_API_KEYS" in script
    assert "KNOWLEDGE_CHAT_INBOUND_API_KEYS=" in chat
    assert "RAG_API_KEYS" not in script
    assert "RAG_API_KEYS" not in chat


def test_internal_dependencies_are_health_gated() -> None:
    services = _compose()["services"]
    assert services["repo-doc-ingestion"]["depends_on"] == {
        "mineru-api": {"condition": "service_healthy"}
    }
    expected = {"rag-retrieval-service": {"condition": "service_healthy"}}
    assert services["rag-knowledge-chat"]["depends_on"] == expected
    assert services["rag-report-agent"]["depends_on"] == expected


def test_env_examples_exist_and_real_env_files_are_never_tracked() -> None:
    for expected in EXPECTED.values():
        env_path = ROOT / expected["env"]
        example_path = env_path.with_suffix(".env.example")
        assert example_path.is_file()
        if not env_path.exists():
            continue
        assert env_path.is_file()
        ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", str(env_path.relative_to(ROOT))],
            cwd=ROOT,
            check=False,
        )
        assert ignored.returncode == 0


def test_deploy_script_restricts_real_env_file_permissions_before_validation() -> None:
    script = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")

    assert 'chmod 600 -- "${ENV_FILES[@]}"' in script
    assert 'fail "Environment file permissions must be 600: $file"' in script


def test_env_templates_use_compose_dns_and_external_database_placeholders() -> None:
    ingestion = (ROOT / "env/ingestion.env.example").read_text(encoding="utf-8")
    retrieval = (ROOT / "env/retrieval.env.example").read_text(encoding="utf-8")
    chat = (ROOT / "env/knowledge-chat.env.example").read_text(encoding="utf-8")
    report = (ROOT / "env/report-agent.env.example").read_text(encoding="utf-8")

    assert "MINERU_API_URL=http://mineru-api:8135" in ingestion
    assert "AUTH_ENABLED=false" in chat
    assert "KNOWLEDGE_CHAT_INBOUND_API_KEYS=\n" in chat
    assert "RAG_RETRIEVAL_SERVICE_URL=http://rag-retrieval-service:8120" in chat
    assert "RAG_RETRIEVAL_SERVICE_URL=http://rag-retrieval-service:8120" in report
    assert "RAG_RETRIEVAL_SERVICE_API_KEY" not in chat
    assert "RAG_RETRIEVAL_SERVICE_API_KEY" not in report
    assert "RAG_SERVICE_API_KEYS" not in retrieval
    assert "POSTGRES_DSN=postgresql://<user>:<password>@<host>:5432/<database>" in ingestion
    assert "POSTGRES_DSN=postgresql://<user>:<password>@<host>:5432/<database>" in retrieval
    assert "PG_DSN=postgresql://<user>:<password>@<host>:5432/<database>" in report


def test_all_service_builds_cover_slow_external_downloads_with_mirrors() -> None:
    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
    dockerfiles = {
        name: (ROOT / expected["context"] / "Dockerfile").read_text(encoding="utf-8")
        for name, expected in EXPECTED.items()
    }

    assert "docker.m.daocloud.io/library/python:3.12-slim" in compose
    assert "docker.m.daocloud.io/library/python:3.12-slim-bookworm" in compose
    assert "https://mirrors.aliyun.com/debian" in compose
    assert "https://pypi.tuna.tsinghua.edu.cn/simple" in compose
    assert "https://mirrors.tuna.tsinghua.edu.cn/pytorch-wheels/cpu" in compose

    for name, dockerfile in dockerfiles.items():
        assert "ARG PYTHON_BASE_IMAGE=" in dockerfile, name
        assert "FROM ${PYTHON_BASE_IMAGE}" in dockerfile, name

    for name in ("repo-doc-ingestion", "rag-report-agent"):
        dockerfile = dockerfiles[name]
        assert "ARG DEBIAN_MIRROR=" in dockerfile
        assert "ARG DEBIAN_SECURITY_MIRROR=" in dockerfile
        assert "ARG PIP_INDEX_URL=" in dockerfile
        assert 'pip config set global.index-url "${PIP_INDEX_URL}"' in dockerfile

    for name in ("rag-retrieval-service", "rag-knowledge-chat"):
        dockerfile = dockerfiles[name]
        lockfile = (ROOT / EXPECTED[name]["context"] / "uv.lock").read_text(
            encoding="utf-8"
        )
        assert "ghcr.io/astral-sh/uv" not in dockerfile
        assert "ARG UV_DEFAULT_INDEX=" in dockerfile
        assert "python -m pip install" in dockerfile
        assert '"uv==${UV_VERSION}"' in dockerfile
        assert "uv sync --frozen --no-dev --no-install-project" in dockerfile
        assert "https://pypi.tuna.tsinghua.edu.cn/simple" in lockfile
        assert "https://pypi.org/simple" not in lockfile
        assert "https://files.pythonhosted.org" not in lockfile


def test_retrieval_admission_is_not_duplicated_by_uvicorn_global_limit() -> None:
    dockerfile = (ROOT / "src/rag-retrieval-service/Dockerfile").read_text(encoding="utf-8")

    assert "--limit-concurrency" not in dockerfile


def test_wechat_article_build_rewrites_locked_pypi_urls_to_configured_mirror() -> None:
    adapter = (ROOT / "src/wechat-article-agent/Dockerfile").read_text(encoding="utf-8")
    runtime = (ROOT / "src/wechat-article-agent/Runtime.Dockerfile").read_text(encoding="utf-8")

    assert "PYPI_MIRROR_BASE=" in adapter
    assert "files.pythonhosted.org/packages" in adapter
    assert "pypi.org/simple" in adapter
    assert "uv sync --frozen --no-dev --no-install-project" in adapter

    assert "PYPI_MIRROR_BASE=" in runtime
    assert "UV_INDEX_URL=${PYPI_MIRROR_BASE}/simple" in runtime
    assert '--index-url "${UV_INDEX_URL}"' in runtime
    assert "ENTRYPOINT []" in runtime
    assert 'CMD ["python", "-m", "langgraph_api.cli"' in runtime
    assert '"--runtime-edition", "inmem"' in runtime
    assert "/deps/wechat-article-agent/langgraph.runtime.json" in runtime

    runtime_config = json.loads(
        (ROOT / "src/wechat-article-agent/langgraph.runtime.json").read_text(encoding="utf-8")
    )
    assert runtime_config["checkpointer"]["path"].startswith("/deps/wechat-article-agent/")
    assert all(path.startswith("/deps/wechat-article-agent/") for path in runtime_config["graphs"].values())


def test_every_env_variable_is_documented() -> None:
    detail_docs = {
        "mineru": ROOT / "src/MinerU/docs/environment-variables.md",
        "ingestion": ROOT / "src/repo-doc-ingestion/docs/environment-variables.md",
        "retrieval": ROOT / "src/rag-retrieval-service/docs/environment-variables.md",
        "knowledge-chat": ROOT / "src/rag-knowledge-chat/docs/environment-variables.md",
        "report-agent": ROOT / "src/rag-report-agent/docs/environment-variables.md",
    }
    platform_doc = (ROOT / "docs/environment-variables.md").read_text(encoding="utf-8")
    for name, detail_path in detail_docs.items():
        documented = platform_doc + detail_path.read_text(encoding="utf-8")
        template = (ROOT / f"env/{name}.env.example").read_text(encoding="utf-8")
        keys = [
            line.split("=", 1)[0]
            for line in template.splitlines()
            if line and not line.startswith("#") and "=" in line
        ]
        assert not [key for key in keys if key not in documented]


def test_deploy_script_is_the_only_platform_entrypoint() -> None:
    script = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    assert "docker compose" in script
    assert "config --quiet" in script
    assert "--rebuild-mineru" in script
    assert 'docker image inspect "$MINERU_IMAGE"' in script
    assert '"${COMPOSE[@]}" build "${build_services[@]}"' in script
    assert '"${COMPOSE[@]}" up -d --remove-orphans' in script
    assert "up -d --build --remove-orphans" not in script
    assert "src/" not in script
    assert not re.search(r"source\s+.*\.env", script)


def test_deploy_script_has_valid_bash_syntax() -> None:
    result = subprocess.run(
        ["bash", "-n", str(ROOT / "scripts/deploy.sh")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_deploy_script_rejects_unknown_arguments_cleanly() -> None:
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/deploy.sh"), "--not-a-real-option"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert result.stderr == "ERROR: Unknown argument: --not-a-real-option\n"


def test_source_snapshot_excludes_runtime_and_repository_artifacts() -> None:
    result = subprocess.run(
        ["git", "ls-files", "src"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    tracked = [Path(line) for line in result.stdout.splitlines()]
    forbidden_dirs = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
    assert not any(forbidden_dirs.intersection(path.parts) for path in tracked)
    assert not any(path.name == ".env" for path in tracked)
    assert not any(path.suffix in {".orig", ".rej", ".pyc"} for path in tracked)


def test_mineru_snapshot_contains_model_python_package() -> None:
    mineru = ROOT / "src/MinerU/mineru"
    for relative_path in (
        "model/__init__.py",
        "model/docx/main.py",
        "model/layout/pp_doclayoutv2.py",
        "model/ocr/pytorch_paddle.py",
    ):
        assert (mineru / relative_path).is_file(), relative_path


def test_mineru_dockerignore_never_excludes_model_directories() -> None:
    patterns = (
        ROOT / "src/MinerU/.dockerignore"
    ).read_text(encoding="utf-8").splitlines()
    assert not {"model", "models", "/model", "/models"}.intersection(patterns)


def test_every_service_docker_context_excludes_real_env_files() -> None:
    contexts = {Path(expected["context"]) for expected in EXPECTED.values()}
    for context in contexts:
        patterns = (
            ROOT / context / ".dockerignore"
        ).read_text(encoding="utf-8").splitlines()
        assert ".env" in patterns, context


def test_mineru_image_checks_required_imports_during_build() -> None:
    dockerfile = (ROOT / "src/MinerU/Dockerfile").read_text(encoding="utf-8")
    assert "import mineru.cli.fast_api" in dockerfile
    assert "import mineru.model" in dockerfile


def test_mineru_installs_heavy_dependencies_before_copying_source() -> None:
    dockerfile = (ROOT / "src/MinerU/Dockerfile").read_text(encoding="utf-8")
    dependency_install = dockerfile.index("/tmp/mineru-requirements.txt")
    source_copy = dockerfile.index("COPY mineru ./mineru")
    project_install = dockerfile.index("--no-deps -e .")

    assert dependency_install < source_copy < project_install


def test_mineru_result_path_helper_supports_api_fallbacks(tmp_path: Path) -> None:
    helpers = runpy.run_path(str(ROOT / "src/MinerU/mineru/cli/output_paths.py"))
    text_dir = tmp_path / "sample" / "text"
    text_dir.mkdir(parents=True)

    resolved = helpers["resolve_parse_dir"](
        tmp_path,
        "sample",
        "pipeline",
        "auto",
        allow_office_fallback=True,
        allow_text_fallback=True,
        allow_html_fallback=True,
    )
