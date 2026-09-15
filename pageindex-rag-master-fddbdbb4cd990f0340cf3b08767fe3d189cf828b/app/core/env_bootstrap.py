"""运行时环境引导：加载 .env 并兼容 ARK/OpenAI 变量。"""

from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv_file(repo_root: Path) -> None:
    """加载 .env（优先 python-dotenv，失败时回退手动解析，支持 UTF-8 BOM）。"""
    env_path = repo_root / ".env"
    if not env_path.exists() or not env_path.is_file():
        return

    try:
        from dotenv import load_dotenv  # type: ignore

        # 先让 python-dotenv 处理常规解析。
        load_dotenv(dotenv_path=env_path, override=False)
    except Exception:
        pass
    try:
        # 再手动解析一遍，用于“环境中已有空值变量”场景：空值应被 .env 非空值填充。
        text = env_path.read_text(encoding="utf-8-sig")
    except Exception:
        return

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
        current = str(os.environ.get(key, "") or "").strip()
        if not current:
            os.environ[key] = value


def _bridge_ark_to_openai() -> None:
    """当仅配置 ARK_* 时，自动补齐 OpenAI 兼容环境变量。"""
    ark_key = str(os.getenv("ARK_API_KEY", "") or "").strip()
    openai_key = str(os.getenv("OPENAI_API_KEY", "") or "").strip()
    if ark_key and not openai_key:
        os.environ["OPENAI_API_KEY"] = ark_key

    ark_base = str(os.getenv("ARK_BASE_URL", "") or "").strip()
    openai_base = str(os.getenv("OPENAI_BASE_URL", "") or "").strip()
    if ark_base and not openai_base:
        os.environ["OPENAI_BASE_URL"] = ark_base


def bootstrap_runtime_env(repo_root: Path) -> None:
    """统一执行运行时环境引导。"""
    _load_dotenv_file(repo_root)
    _bridge_ark_to_openai()
