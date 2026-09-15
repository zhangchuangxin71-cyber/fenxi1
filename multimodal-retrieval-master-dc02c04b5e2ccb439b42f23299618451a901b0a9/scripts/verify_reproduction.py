#!/usr/bin/env python3
"""验收复现脚本：单元测试 +（可选）embedding API 健康与 CLIP 文本编码。

用法（项目根目录）:
  python scripts/verify_reproduction.py
  python scripts/verify_reproduction.py --base-url http://127.0.0.1:8030
  python scripts/verify_reproduction.py --skip-api   # 仅跑单元测试
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def run_unittests() -> bool:
    print("=== [1/3] 单元测试（无需启动服务、无需模型）===")
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    ok = result.wasSuccessful()
    print("  ->", "PASS" if ok else "FAIL")
    return ok


def check_api(base_url: str, timeout: float = 120.0) -> bool:
    print(f"\n=== [2/3] API 健康检查 {base_url}/health ===")
    try:
        r = requests.get(f"{base_url.rstrip('/')}/health", timeout=10)
        r.raise_for_status()
        body = r.json()
        print(json.dumps(body, ensure_ascii=False, indent=2))
        if body.get("status") != "ok":
            print("  -> FAIL: status 不是 ok")
            return False
        print("  -> PASS")
    except requests.RequestException as exc:
        print(f"  -> SKIP/FAIL: 无法连接服务 ({exc})")
        print("  请先启动: python start_embedding_service.py")
        return False

    print(f"\n=== [3/3] CLIP 文本向量 POST /v1/query/clip/embed ===")
    try:
        r = requests.post(
            f"{base_url.rstrip('/')}/v1/query/clip/embed",
            json={"text": "工地上作业的工人", "return_vector": True},
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        vec = data.get("embedding") or []
        dim = len(vec)
        print(f"  text=工地上作业的工人  dim={dim}")
        if dim <= 0:
            print("  -> FAIL: embedding 为空")
            return False
        print("  -> PASS")
        return True
    except requests.RequestException as exc:
        print(f"  -> FAIL: {exc}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("EMBEDDING_BASE_URL", "http://127.0.0.1:8030"),
    )
    parser.add_argument("--skip-api", action="store_true", help="仅运行单元测试")
    args = parser.parse_args()

    load_dotenv()
    port = os.environ.get("EMBEDDING_SERVICE_PORT", "8030")
    base = args.base_url
    if "8030" not in base and port:
        base = f"http://127.0.0.1:{port}"

    unit_ok = run_unittests()
    api_ok = True
    if not args.skip_api:
        api_ok = check_api(base)
    else:
        print("\n=== [2/3][3/3] 已跳过 API 检查 (--skip-api) ===")

    print("\n" + "=" * 60)
    if unit_ok and api_ok:
        print("验收通过。完整说明见 QUICKSTART.md")
        return 0
    if unit_ok and not api_ok:
        print("单元测试通过；API 未就绪或未启动。启动服务后重跑本脚本。")
        print("详见 QUICKSTART.md 第三节。")
        return 1
    print("验收未通过，请根据上方输出排查。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
