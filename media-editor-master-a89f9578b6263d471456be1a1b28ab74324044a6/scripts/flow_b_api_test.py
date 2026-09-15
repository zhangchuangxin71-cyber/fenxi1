#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Flow B 四接口一体化测试（单文件版）

覆盖接口
--------
  split           分句        POST /api/v1/split/run
  audio_process   配音        POST /api/v1/audio/process/run
  visual_preview  字幕预览    POST /api/v1/visual/preview/run
  compose         视频合成    POST /api/v1/compose/run

每条用例：POST 提交 → 轮询 GET 任务状态 → 断言是否符合 expect。
报告写入 docs/flow_b_api_test_report.md（按接口分组，含 request_body / submit_response / poll_result）。

前置条件
--------
  1. FastAPI 已启动（默认 http://127.0.0.1:8787）
  2. OSS 桶内已上传测试素材目录 flow-b-api-test/（见下方「OSS 目录约定」）
  3. pip install requests

基本用法
--------
  # 跑全部单接口（compose 较慢，建议首次加 --skip-compose）
  python3 scripts/flow_b_api_test.py --skip-compose

  # 跑全部含合成
  python3 scripts/flow_b_api_test.py

  # 只跑某一个接口
  python3 scripts/flow_b_api_test.py --interface split
  python3 scripts/flow_b_api_test.py --interface audio_process
  python3 scripts/flow_b_api_test.py --interface visual_preview
  python3 scripts/flow_b_api_test.py --interface compose

  # 多个接口可重复指定
  python3 scripts/flow_b_api_test.py --interface split --interface audio_process

命令行参数
----------
  --interface NAME    只跑指定接口（split / audio_process / visual_preview / compose），可重复
  --case-id REGEX     按用例 id 正则过滤，如 --case-id split_01
  --expect TYPE       按期望类型过滤（当前用例均为 success）
  --skip-compose      跳过 compose 用例（默认由 SKIP_COMPOSE_DEFAULT 控制）
  -v, --verbose       打印完整 submit / poll JSON（失败用例默认也会打印详情）
  --fail-fast         遇到第一个失败用例立即停止

常用组合示例
------------
  # 快速冒烟：分句 + 一条配音
  python3 scripts/flow_b_api_test.py --interface split --case-id split_01

  # 只跑成功路径（不含 compose）
  python3 scripts/flow_b_api_test.py --skip-compose

  # 联调某条用例并看完整响应
  python3 scripts/flow_b_api_test.py --case-id compose_01 -v

配置说明
--------
  .env（与 API 服务共用，见 .env.example）
    ALIYUN_OSS_BUCKET    OSS 桶名
    ALIYUN_OSS_ENDPOINT  OSS 区域 endpoint（如 oss-cn-shenzhen.aliyuncs.com）

  文件顶部常量
    API_BASE             FastAPI 地址
    OSS_TEST_ROOT        桶内测试素材根目录（默认 prod/ImagesVideosText2Video/demo-user-001/flow-b-api-test）
    REPORT_FILE          Markdown 报告路径
    SKIP_COMPOSE_DEFAULT 是否默认跳过 compose

  测试用例在文件底部 TEST_CASES（JSON 数组），body 可用占位符（均为 OSS object key）：
    ${audio_key:1}                → flow-b-api-test/audio/1.wav
    ${image_key:2}                → flow-b-api-test/images/s0000002.jpg
    ${video_key:1}                → flow-b-api-test/video/1.mp4
    ${deliverable.coffee.master_key} → flow-b-api-test/deliverables/coffee_master.wav

  换环境时：复制 flow-b-api-test/ 到新桶，并更新 .env 中的 ALIYUN_OSS_BUCKET。

退出码
------
  0  全部通过
  1  有用例失败
  2  健康检查失败 / 无匹配用例
 130  Ctrl+C 中断
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# =============================================================================
# 配置区
# =============================================================================

_REPO_ROOT = Path(__file__).resolve().parents[1]

API_BASE = "http://127.0.0.1:8787"  # FastAPI 服务地址

# 桶内统一测试目录 —— 所有素材都放在这个文件夹下，子目录结构固定
OSS_TEST_ROOT = "prod/ImagesVideosText2Video/demo-user-001/flow-b-api-test"
#OSS_TEST_ROOT = "flow-b-api-test"
def _load_dotenv() -> None:
    """Load project .env (does not override existing env vars)."""
    env_file = _REPO_ROOT / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _normalize_oss_endpoint(raw: str) -> str:
    return raw.strip().removeprefix("https://").removeprefix("http://").rstrip("/")


def _configure_oss_from_env() -> None:
    global OSS_BUCKET, OSS_ENDPOINT, OSS_HTTPS_BASE
    _load_dotenv()
    OSS_BUCKET = os.environ.get("ALIYUN_OSS_BUCKET", "").strip()
    OSS_ENDPOINT = _normalize_oss_endpoint(
        os.environ.get("ALIYUN_OSS_ENDPOINT", "oss-cn-shenzhen.aliyuncs.com")
    )
    if not OSS_BUCKET:
        raise SystemExit(
            "ALIYUN_OSS_BUCKET not set; configure in .env (see .env.example)"
        )
    OSS_HTTPS_BASE = f"https://{OSS_BUCKET}.{OSS_ENDPOINT}"


OSS_BUCKET = ""
OSS_ENDPOINT = ""
OSS_HTTPS_BASE = ""
_configure_oss_from_env()

# 报告输出路径（相对项目根，仅生成 Markdown）
REPORT_FILE = "docs/flow_b_api_test_report.md"

# 默认是否跳过 compose（耗时长）
SKIP_COMPOSE_DEFAULT = False

# -----------------------------------------------------------------------------
# OSS 目录约定（把文件上传到桶内 flow-b-api-test/ 下，结构如下）:
#
#   flow-b-api-test/
#     audio/1.wav … 14.wav          配音输入 + BGM（14.wav）
#     images/s0000001.jpg …         预览/合成图片（至少 1~4）
#     video/1.mp4                   预览 video 用例
#     deliverables/
#       coffee_master.wav / coffee_subtitle.srt
#       fitness_master.wav / fitness_subtitle.srt
#       reading_master.wav / reading_subtitle.srt
#
# 换桶：整目录复制 flow-b-api-test/ 到新桶即可。
# -----------------------------------------------------------------------------

# 配音产物文件名（相对 flow-b-api-test/deliverables/）
_DELIVERABLE_FILES: dict[str, tuple[str, str]] = {
    "coffee": ("coffee_master.wav", "coffee_subtitle.srt"),
    "fitness": ("fitness_master.wav", "fitness_subtitle.srt"),
    "reading": ("reading_master.wav", "reading_subtitle.srt"),
}

# =============================================================================
# 以下一般不用改
# =============================================================================

EXPECT_DONE = "提交成功（HTTP 200），轮询至任务完成（status=done）"
EXPECT_VALIDATION = "提交失败，参数校验不通过（HTTP 422，code=40001）"
EXPECT_POLL_FAILED = "提交成功（HTTP 200），轮询至任务失败（status=failed，多为 URL 不可达）"

POLL = {
    "split": {"interval": 2, "timeout": 300, "submit_timeout": 120},
    "audio_process": {"interval": 3, "timeout": 900, "submit_timeout": 180},
    "visual_preview": {"interval": 3, "timeout": 600, "submit_timeout": 120},
    "compose": {"interval": 5, "timeout": 3600, "submit_timeout": 120},
}

INTERFACE_META = {
    "split": {
        "label": "分句",
        "method": "POST",
        "path": "/api/v1/split/run",
        "poll_method": "GET",
        "poll_path_template": "/api/v1/split/tasks/{task_id}",
    },
    "audio_process": {
        "label": "配音",
        "method": "POST",
        "path": "/api/v1/audio/process/run",
        "poll_method": "GET",
        "poll_path_template": "/api/v1/audio/process/tasks/{task_id}",
    },
    "visual_preview": {
        "label": "字幕预览",
        "method": "POST",
        "path": "/api/v1/visual/preview/run",
        "poll_method": "GET",
        "poll_path_template": "/api/v1/visual/preview/tasks/{task_id}",
    },
    "compose": {
        "label": "视频合成",
        "method": "POST",
        "path": "/api/v1/compose/run",
        "poll_method": "GET",
        "poll_path_template": "/api/v1/compose/tasks/{task_id}",
    },
}

EXPECT_LABELS = {
    EXPECT_DONE: "成功用例",
    EXPECT_VALIDATION: "参数校验",
    EXPECT_POLL_FAILED: "资源不可达",
}

INTERFACE_LABELS = {k: v["label"] for k, v in INTERFACE_META.items()}

_PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")


def _storage() -> dict[str, Any]:
    return {"oss_https_base": OSS_HTTPS_BASE.rstrip("/"), "oss_test_root": OSS_TEST_ROOT}


def _join_key(*parts: str) -> str:
    return "/".join(p.strip("/") for p in parts if p)


def _oss_key(relative: str) -> str:
    """桶内 object key：flow-b-api-test/audio/1.wav"""
    return _join_key(OSS_TEST_ROOT, relative)


def _https(key: str) -> str:
    return f"{OSS_HTTPS_BASE.rstrip('/')}/{key.lstrip('/')}"


def _oss_https(relative: str) -> str:
    return _https(_oss_key(relative))


def _image_name(index: int) -> str:
    return f"s{int(index):07d}.jpg"


def resolve_token(token: str) -> str:
    token = token.strip()
    if token == "oss_https_base":
        return OSS_HTTPS_BASE.rstrip("/")

    m = re.fullmatch(r"audio_https:(\d+)", token)
    if m:
        return _oss_https(f"audio/{m.group(1)}.wav")
    m = re.fullmatch(r"audio_key:(\d+)", token)
    if m:
        return _oss_key(f"audio/{m.group(1)}.wav")
    m = re.fullmatch(r"image_https:(\d+)", token)
    if m:
        return _oss_https(f"images/{_image_name(int(m.group(1)))}")
    m = re.fullmatch(r"image_key:(\d+)", token)
    if m:
        return _oss_key(f"images/{_image_name(int(m.group(1)))}")
    m = re.fullmatch(r"video_https:(\d+)", token)
    if m:
        return _oss_https(f"video/{m.group(1)}.mp4")
    m = re.fullmatch(r"video_key:(\d+)", token)
    if m:
        return _oss_key(f"video/{m.group(1)}.mp4")

    m = re.fullmatch(
        r"deliverable\.([a-z0-9_]+)\.(master|srt|master_key|srt_key|master_https|srt_https)",
        token,
    )
    if m:
        fid, field = m.group(1), m.group(2)
        master_name, srt_name = _DELIVERABLE_FILES[fid]
        if field in ("master", "master_https"):
            return _oss_https(f"deliverables/{master_name}")
        if field in ("srt", "srt_https"):
            return _oss_https(f"deliverables/{srt_name}")
        if field == "master_key":
            return _oss_key(f"deliverables/{master_name}")
        if field == "srt_key":
            return _oss_key(f"deliverables/{srt_name}")
    raise KeyError(f"unknown placeholder: {token}")


def resolve_body(obj: Any) -> Any:
    if isinstance(obj, str):
        if "${" not in obj:
            return obj
        return _PLACEHOLDER.sub(lambda m: resolve_token(m.group(1)), obj)
    if isinstance(obj, list):
        return [resolve_body(x) for x in obj]
    if isinstance(obj, dict):
        return {k: resolve_body(v) for k, v in obj.items()}
    return obj


def api_request(method: str, path: str, body: dict | None = None, timeout: float = 120):
    url = f"{API_BASE.rstrip('/')}{path}"
    resp = requests.request(
        method,
        url,
        json=body,
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )
    try:
        payload = resp.json()
    except Exception:
        payload = {"code": resp.status_code, "message": resp.text, "data": {}}
    return resp.status_code, payload, dict(resp.headers)


def poll_until_done(poll_path: str, iface: str, verbose: bool) -> tuple[int, dict, dict]:
    cfg = POLL[iface]
    deadline = time.time() + cfg["timeout"]
    last_status, last_payload, last_headers = 0, {}, {}
    while time.time() < deadline:
        last_status, last_payload, last_headers = api_request("GET", poll_path, timeout=60)
        if last_status != 200 or last_payload.get("code") != 0:
            raise RuntimeError(f"poll failed: HTTP {last_status} {last_payload}")
        data = last_payload.get("data") or {}
        st = data.get("status")
        if verbose:
            prog = data.get("progress") or {}
            print(f"      poll {st} {prog.get('percent', '')}% {prog.get('message') or ''}")
        if st in ("done", "failed", "cancelled"):
            return last_status, last_payload, last_headers
        time.sleep(cfg["interval"])
    raise TimeoutError(f"poll timeout, last status={data.get('status')}")


def expect_kind(expect: str) -> str:
    if expect == EXPECT_DONE:
        return "done"
    if expect == EXPECT_VALIDATION:
        return "validation"
    if expect == EXPECT_POLL_FAILED:
        return "poll_failed"
    raise ValueError(expect)


def summarize_result(iface: str, result: dict | None) -> dict:
    if not result:
        return {}
    if iface == "split":
        return {k: result.get(k) for k in ("text_digest", "segment_count", "split_mode_used")}
    if iface == "audio_process":
        return {k: result.get(k) for k in ("segment_count", "master_audio_url", "subtitle_srt_url", "total_with_gaps_sec")}
    if iface == "visual_preview":
        return {k: result.get(k) for k in ("preview_image_url", "resolution", "subtitle_style_applied")}
    if iface == "compose":
        return {k: result.get(k) for k in ("output_video_url", "output_audio_url", "log_url")}
    return {}


INTERFACE_DEFAULT_ASSERT: dict[str, dict[str, Any]] = {
    "split": {"segment_count_min": 1, "has_keys": ["text_digest"]},
    "audio_process": {
        "has_keys": ["master_audio_url", "subtitle_srt_url"],
        "https_keys": ["master_audio_url", "subtitle_srt_url"],
    },
    "visual_preview": {"has_keys": ["preview_image_url"], "https_keys": ["preview_image_url"]},
    "compose": {
        "has_keys": ["output_video_url", "output_audio_url", "log_url"],
        "https_keys": ["output_video_url", "output_audio_url", "log_url"],
    },
}


def _merge_assert_spec(iface: str, case_assert: dict[str, Any] | None) -> dict[str, Any]:
    base = dict(INTERFACE_DEFAULT_ASSERT.get(iface, {}))
    if case_assert:
        base.update(case_assert)
    return base


def check_poll_result(iface: str, result: dict | None, assert_spec: dict[str, Any] | None) -> str | None:
    """Return error message if assertion fails, else None."""
    if not result:
        return "poll result is empty"
    spec = _merge_assert_spec(iface, assert_spec)

    seg_min = spec.get("segment_count_min")
    if seg_min is not None:
        count = result.get("segment_count")
        if count is None and isinstance(result.get("segments"), list):
            count = len(result["segments"])
        if count is None or count < seg_min:
            return f"segment_count {count!r} < min {seg_min}"

    seg_eq = spec.get("segment_count")
    if seg_eq is not None and result.get("segment_count") != seg_eq:
        return f"segment_count expected {seg_eq}, got {result.get('segment_count')!r}"

    for key in spec.get("has_keys") or []:
        if not result.get(key):
            return f"missing result.{key}"

    for key in spec.get("https_keys") or []:
        val = result.get(key)
        if not isinstance(val, str) or not val.startswith("https://"):
            return f"result.{key} is not https URL: {val!r}"

    return None


# =============================================================================
# 测试用例 —— 可直接增删改 JSON 条目
#
# body 占位符会拼到 OSS_TEST_ROOT 下（见顶部目录约定，均为 OSS object key）:
#   ${audio_key:1}     → flow-b-api-test/audio/1.wav
#   ${image_key:1}     → flow-b-api-test/images/s0000001.jpg
#   ${video_key:1}     → flow-b-api-test/video/1.mp4
#   ${deliverable.coffee.master_key} → flow-b-api-test/deliverables/coffee_master.wav
#
# 可选 assert_result（与 INTERFACE_DEFAULT_ASSERT 合并）:
#   segment_count_min / segment_count / has_keys / https_keys
#
# expect: 提交成功（HTTP 200），轮询至任务完成（status=done）
# =============================================================================

TEST_CASES: list[dict[str, Any]] = json.loads(
"""
[
  {
    "interface": "split",
    "id": "split_01_short_text_no_style",
    "name": "短文案 · 不传 global_style",
    "expect": "提交成功（HTTP 200），轮询至任务完成（status=done）",
    "path": "/api/v1/split/run",
    "poll_path": "/api/v1/split/tasks/{task_id}",
    "body": {
      "text": "春天来了，万物复苏。小草探出脑袋，花儿竞相开放。"
    }
  },
  {
    "interface": "split",
    "id": "split_02_travel_long_text_no_style",
    "name": "旅行 vlog 长文案 · 不传 global_style",
    "expect": "提交成功（HTTP 200），轮询至任务完成（status=done）",
    "path": "/api/v1/split/run",
    "poll_path": "/api/v1/split/tasks/{task_id}",
    "body": {
      "text": "周末清晨，我独自踏上了前往山里的旅程。沿着蜿蜒的公路前行，窗外的风景从繁华都市渐渐过渡到宁静田野。到达山脚时，薄雾还未散去，空气里带着露水的清冽。踩着石阶向上，每一步都能听见心跳与鸟鸣交织。登顶那一刻，云海在脚下翻涌，所有的疲惫都被风吹散。",
      "include_ai_prompts": false
    }
  },
  {
    "interface": "split",
    "id": "split_03_product_seed_text_no_style",
    "name": "产品种草文案 · 不传 global_style",
    "expect": "提交成功（HTTP 200），轮询至任务完成（status=done）",
    "path": "/api/v1/split/run",
    "poll_path": "/api/v1/split/tasks/{task_id}",
    "body": {
      "text": "这款蓝牙耳机续航长达三十小时，降噪效果出色，通勤路上再也不怕嘈杂。轻量化设计，佩戴一整天也不累。现在下单还有限时优惠，喜欢的朋友别错过。"
    }
  },
  {
    "interface": "split",
    "id": "split_04_with_global_style",
    "name": "长文案 · include_ai_prompts=true · 传 global_style",
    "expect": "提交成功（HTTP 200），轮询至任务完成（status=done）",
    "path": "/api/v1/split/run",
    "poll_path": "/api/v1/split/tasks/{task_id}",
    "body": {
      "text": "传统手工艺正在年轻一代手中焕发新生。从选料到成型，每一道工序都凝聚着匠人数十年的经验。镜头记录下指尖的温度，也记录下一种不愿消失的生活方式。",
      "include_ai_prompts": true,
      "global_style": "纪实风格，匠人特写，暖色室内光"
    }
  },
  {
    "interface": "split",
    "id": "split_05_ai_prompts_default_style",
    "name": "科技解说文案 · include_ai_prompts=true · 不传 global_style（服务端默认「电影质感」）",
    "expect": "提交成功（HTTP 200），轮询至任务完成（status=done）",
    "path": "/api/v1/split/run",
    "poll_path": "/api/v1/split/tasks/{task_id}",
    "body": {
      "text": "深海之下藏着人类尚未揭开的秘密。探测器缓缓下潜，舷窗外是幽蓝与漆黑交织的世界。每一次新发现，都在改写我们对生命起源的认知。",
      "include_ai_prompts": true
    }
  },
  {
    "interface": "audio_process",
    "id": "audio_01_oss_key_all_text",
    "name": "正常 · 咖啡主题 · 全句传 text · OSS key",
    "expect": "提交成功（HTTP 200），轮询至任务完成（status=done）",
    "path": "/api/v1/audio/process/run",
    "poll_path": "/api/v1/audio/process/tasks/{task_id}",
    "body": {
      "code": "e2e",
      "id": "test-coffee",
      "segments": [
        {
          "index": 1,
          "text": "推开木门，烘豆机的暖香扑面而来，",
          "audio": {
            "url": "${audio_key:1}"
          }
        },
        {
          "index": 2,
          "text": "咖啡师手起杯落，拉花在杯面绽开一朵郁金香。",
          "audio": {
            "url": "${audio_key:2}"
          }
        }
      ]
    }
  },
  {
    "interface": "audio_process",
    "id": "audio_02_oss_key",
    "name": "正常 · 健身主题 · OSS object key",
    "expect": "提交成功（HTTP 200），轮询至任务完成（status=done）",
    "path": "/api/v1/audio/process/run",
    "poll_path": "/api/v1/audio/process/tasks/{task_id}",
    "body": {
      "code": "e2e",
      "id": "test-fitness",
      "segments": [
        {
          "index": 1,
          "text": "清晨六点的健身房里，器械声与呼吸声交错。",
          "audio": {
            "url": "${audio_key:1}"
          }
        }
      ]
    }
  },
  {
    "interface": "audio_process",
    "id": "audio_03_oss_key_reading",
    "name": "正常 · 阅读主题 · OSS key · force_refresh + speed",
    "expect": "提交成功（HTTP 200），轮询至任务完成（status=done）",
    "path": "/api/v1/audio/process/run",
    "poll_path": "/api/v1/audio/process/tasks/{task_id}",
    "body": {
      "code": "e2e",
      "id": "test-reading",
      "force_refresh": true,
      "speed": 1.2,
      "segments": [
        {
          "index": 1,
          "text": "翻开这本书的第一页，故事从一场细雨中的相遇开始，",
          "audio": {
            "url": "${audio_key:1}"
          }
        },
        {
          "index": 2,
          "text": "作者用细腻的笔触，把人物的命运悄悄系在了一起。",
          "audio": {
            "url": "${audio_key:2}"
          }
        }
      ]
    }
  },
  {
    "interface": "audio_process",
    "id": "audio_04_four_segments_slow",
    "name": "咖啡主题 · 4 句 · speed=0.8",
    "expect": "提交成功（HTTP 200），轮询至任务完成（status=done）",
    "path": "/api/v1/audio/process/run",
    "poll_path": "/api/v1/audio/process/tasks/{task_id}",
    "assert_result": {
      "segment_count": 4
    },
    "body": {
      "code": "e2e",
      "id": "test-coffee-4seg",
      "speed": 0.8,
      "segments": [
        {
          "index": 1,
          "text": "推开木门，烘豆机的暖香扑面而来，",
          "audio": {
            "url": "${audio_key:1}"
          }
        },
        {
          "index": 2,
          "text": "咖啡师手起杯落，拉花在杯面绽开一朵郁金香。",
          "audio": {
            "url": "${audio_key:2}"
          }
        },
        {
          "index": 3,
          "text": "窗边座位洒满午后阳光，",
          "audio": {
            "url": "${audio_key:3}"
          }
        },
        {
          "index": 4,
          "text": "一杯手冲，便是这座城市最温柔的停顿。",
          "audio": {
            "url": "${audio_key:4}"
          }
        }
      ]
    }
  },
  {
    "interface": "visual_preview",
    "id": "preview_01_image_oss_key_default_style",
    "name": "正常 · 城市日落 · 图片 OSS key · 竖屏",
    "expect": "提交成功（HTTP 200），轮询至任务完成（status=done）",
    "path": "/api/v1/visual/preview/run",
    "poll_path": "/api/v1/visual/preview/tasks/{task_id}",
    "body": {
      "code": "e2e",
      "id": "noodle-stateless",
      "media_url": "${image_key:1}",
      "media_type": "image",
      "text": "城市的天际线在落日余晖中渐渐镀上一层金边，",
      "resolution": {
        "width": 1080,
        "height": 1920
      },
      "subtitle_style": {
        "font_name": "思源黑体",
        "font_scale": 1.0,
        "y_offset": 0
      }
    }
  },
  {
    "interface": "visual_preview",
    "id": "preview_02_video_oss_key_y_offset_minus1",
    "name": "正常 · 海岸风光 · 视频 OSS key · y_offset=-1",
    "expect": "提交成功（HTTP 200），轮询至任务完成（status=done）",
    "path": "/api/v1/visual/preview/run",
    "poll_path": "/api/v1/visual/preview/tasks/{task_id}",
    "body": {
      "code": "e2e",
      "id": "noodle-stateless",
      "media_url": "${video_key:1}",
      "media_type": "video",
      "start_sec": 2.5,
      "text": "镜头缓缓掠过海浪拍打礁石的瞬间，",
      "resolution": {
        "width": 1080,
        "height": 1920
      },
      "subtitle_style": {
        "font_name": "思源黑体",
        "font_scale": 1.2,
        "y_offset": -1
      }
    }
  },
  {
    "interface": "visual_preview",
    "id": "preview_03_oss_key_image",
    "name": "正常 · 美食主题 · OSS object key · 图片",
    "expect": "提交成功（HTTP 200），轮询至任务完成（status=done）",
    "path": "/api/v1/visual/preview/run",
    "poll_path": "/api/v1/visual/preview/tasks/{task_id}",
    "body": {
      "code": "e2e",
      "id": "noodle-stateless",
      "media_url": "${image_key:1}",
      "media_type": "image",
      "text": "一盘热气腾腾的舒芙蕾被轻轻端上餐桌，",
      "resolution": {
        "width": 1080,
        "height": 1920
      },
      "subtitle_style": {
        "font_name": "迷茫体",
        "font_scale": 1.5,
        "y_offset": 2
      }
    }
  },
  {
    "interface": "visual_preview",
    "id": "preview_04_landscape_resolution",
    "name": "正常 · 数码产品 · 横屏 1920×1080",
    "expect": "提交成功（HTTP 200），轮询至任务完成（status=done）",
    "path": "/api/v1/visual/preview/run",
    "poll_path": "/api/v1/visual/preview/tasks/{task_id}",
    "body": {
      "code": "e2e",
      "id": "noodle-stateless",
      "media_url": "${image_key:1}",
      "media_type": "image",
      "text": "这款轻薄本整机重量不到一千克，随身携带毫无压力。",
      "resolution": {
        "width": 1920,
        "height": 1080
      },
      "subtitle_style": {
        "font_name": "拼搏体",
        "font_scale": 0.8,
        "y_offset": 5
      }
    }
  },
  {
    "interface": "visual_preview",
    "id": "preview_05_custom_resolution_720p",
    "name": "正常 · 课堂科普 · 720×1280 · 文楷",
    "expect": "提交成功（HTTP 200），轮询至任务完成（status=done）",
    "path": "/api/v1/visual/preview/run",
    "poll_path": "/api/v1/visual/preview/tasks/{task_id}",
    "body": {
      "code": "e2e",
      "id": "noodle-stateless",
      "media_url": "${image_key:1}",
      "media_type": "image",
      "text": "孩子们围坐在讲台前，听老师讲述遥远星系的奥秘。",
      "resolution": {
        "width": 720,
        "height": 1280
      },
      "subtitle_style": {
        "font_name": "文楷",
        "font_scale": 2.0,
        "y_offset": -3
      }
    }
  },
  {
    "interface": "visual_preview",
    "id": "preview_06_video_oss_key",
    "name": "正常 · 海岸风光 · 视频 OSS key · start_sec=0.5",
    "expect": "提交成功（HTTP 200），轮询至任务完成（status=done）",
    "path": "/api/v1/visual/preview/run",
    "poll_path": "/api/v1/visual/preview/tasks/{task_id}",
    "body": {
      "code": "e2e",
      "id": "noodle-stateless",
      "media_url": "${video_key:1}",
      "media_type": "video",
      "start_sec": 0.5,
      "text": "镜头缓缓掠过海浪拍打礁石的瞬间，",
      "resolution": {
        "width": 1080,
        "height": 1920
      },
      "subtitle_style": {
        "font_name": "思源黑体",
        "font_scale": 1.0,
        "y_offset": 0
      }
    }
  },
  {
    "interface": "compose",
    "id": "compose_01_minimal_oss_key",
    "name": "正常 · 最简必填 · OSS key · 单句图片",
    "expect": "提交成功（HTTP 200），轮询至任务完成（status=done）",
    "path": "/api/v1/compose/run",
    "poll_path": "/api/v1/compose/tasks/{task_id}",
    "body": {
      "code": "e2e",
      "id": "test-fitness",
      "master_audio_url": "${deliverable.fitness.master_key}",
      "subtitle_srt_url": "${deliverable.fitness.srt_key}",
      "segments": [
        {
          "index": 1,
          "text": "清晨六点的健身房里，器械声与呼吸声交错。",
          "media": {
            "url": "${image_key:1}",
            "type": "image"
          }
        }
      ]
    }
  },
  {
    "interface": "compose",
    "id": "compose_02_full_style_bgm_oss_key",
    "name": "正常 · 完整参数 · OSS key · 硬字幕 + BGM + 样式",
    "expect": "提交成功（HTTP 200），轮询至任务完成（status=done）",
    "path": "/api/v1/compose/run",
    "poll_path": "/api/v1/compose/tasks/{task_id}",
    "body": {
      "code": "e2e",
      "id": "test-coffee",
      "master_audio_url": "${deliverable.coffee.master_key}",
      "subtitle_srt_url": "${deliverable.coffee.srt_key}",
      "voice_volume": 1.0,
      "subtitle_mode": "hard",
      "resolution": {
        "width": 1080,
        "height": 1920
      },
      "subtitle_style": {
        "font_name": "思源黑体",
        "font_scale": 1.2,
        "y_offset": -1
      },
      "bgm": {
        "url": "${audio_key:14}",
        "bgm_volume": 0.15
      },
      "reuse_intermediates": true,
      "cleanup_scratch_on_success": true,
      "segments": [
        {
          "index": 1,
          "text": "推开木门，烘豆机的暖香扑面而来，",
          "media": {
            "url": "${image_key:1}",
            "type": "image"
          }
        },
        {
          "index": 2,
          "text": "咖啡师手起杯落，拉花在杯面绽开一朵郁金香。",
          "media": {
            "url": "${image_key:2}",
            "type": "image"
          }
        }
      ]
    }
  },
  {
    "interface": "compose",
    "id": "compose_03_oss_key_urls",
    "name": "正常 · master/srt/media 混用 OSS object key · 第 3 句",
    "expect": "提交成功（HTTP 200），轮询至任务完成（status=done）",
    "path": "/api/v1/compose/run",
    "poll_path": "/api/v1/compose/tasks/{task_id}",
    "body": {
      "code": "e2e",
      "id": "test-reading",
      "master_audio_url": "${deliverable.reading.master_key}",
      "subtitle_srt_url": "${deliverable.reading.srt_key}",
      "segments": [
        {
          "index": 1,
          "text": "翻开这本书的第一页，故事从一场细雨中的相遇开始，",
          "media": {
            "url": "${image_key:1}",
            "type": "image"
          }
        }
      ]
    }
  },
  {
    "interface": "compose",
    "id": "compose_04_soft_subtitle_image",
    "name": "正常 · 图片素材 · soft 字幕",
    "expect": "提交成功（HTTP 200），轮询至任务完成（status=done）",
    "path": "/api/v1/compose/run",
    "poll_path": "/api/v1/compose/tasks/{task_id}",
    "body": {
      "code": "e2e",
      "id": "test-coffee",
      "master_audio_url": "${deliverable.coffee.master_key}",
      "subtitle_srt_url": "${deliverable.coffee.srt_key}",
      "subtitle_mode": "soft",
      "segments": [
        {
          "index": 1,
          "text": "推开木门，烘豆机的暖香扑面而来，",
          "media": {
            "url": "${image_key:4}",
            "type": "image"
          }
        }
      ]
    }
  },
  {
    "interface": "compose",
    "id": "compose_05_oss_key_bgm_multi_seg",
    "name": "正常 · OSS key master + BGM + 多句",
    "expect": "提交成功（HTTP 200），轮询至任务完成（status=done）",
    "path": "/api/v1/compose/run",
    "poll_path": "/api/v1/compose/tasks/{task_id}",
    "body": {
      "code": "e2e",
      "id": "test-reading",
      "master_audio_url": "${deliverable.reading.master_key}",
      "subtitle_srt_url": "${deliverable.reading.srt_key}",
      "bgm": {
        "url": "${audio_key:14}",
        "bgm_volume": 0.2
      },
      "segments": [
        {
          "index": 1,
          "text": "翻开这本书的第一页，故事从一场细雨中的相遇开始，",
          "media": {
            "url": "${image_key:1}",
            "type": "image"
          }
        },
        {
          "index": 2,
          "text": "作者用细腻的笔触，把人物的命运悄悄系在了一起。",
          "media": {
            "url": "${image_key:3}",
            "type": "image"
          }
        }
      ]
    }
  },
  {
    "interface": "compose",
    "id": "compose_06_single_video_segment",
    "name": "正常 · 健身主题 · 单句 video 素材",
    "expect": "提交成功（HTTP 200），轮询至任务完成（status=done）",
    "path": "/api/v1/compose/run",
    "poll_path": "/api/v1/compose/tasks/{task_id}",
    "body": {
      "code": "e2e",
      "id": "test-fitness",
      "master_audio_url": "${deliverable.fitness.master_key}",
      "subtitle_srt_url": "${deliverable.fitness.srt_key}",
      "segments": [
        {
          "index": 1,
          "text": "清晨六点的健身房里，器械声与呼吸声交错。",
          "media": {
            "url": "${video_key:1}",
            "type": "video",
            "start_sec": 1.0
          }
        }
      ]
    }
  },
  {
    "interface": "compose",
    "id": "compose_07_mixed_image_video",
    "name": "正常 · 阅读主题 · 图文混剪（image + video）",
    "expect": "提交成功（HTTP 200），轮询至任务完成（status=done）",
    "path": "/api/v1/compose/run",
    "poll_path": "/api/v1/compose/tasks/{task_id}",
    "body": {
      "code": "e2e",
      "id": "test-reading",
      "master_audio_url": "${deliverable.reading.master_key}",
      "subtitle_srt_url": "${deliverable.reading.srt_key}",
      "segments": [
        {
          "index": 1,
          "text": "翻开这本书的第一页，故事从一场细雨中的相遇开始，",
          "media": {
            "url": "${image_key:1}",
            "type": "image"
          }
        },
        {
          "index": 2,
          "text": "作者用细腻的笔触，把人物的命运悄悄系在了一起。",
          "media": {
            "url": "${video_key:1}",
            "type": "video",
            "start_sec": 2.0
          }
        }
      ]
    }
  },
  {
    "interface": "compose",
    "id": "compose_08_landscape_soft_bgm",
    "name": "正常 · 横屏 1920×1080 · soft 字幕 · 低 BGM",
    "expect": "提交成功（HTTP 200），轮询至任务完成（status=done）",
    "path": "/api/v1/compose/run",
    "poll_path": "/api/v1/compose/tasks/{task_id}",
    "body": {
      "code": "e2e",
      "id": "test-coffee-landscape",
      "master_audio_url": "${deliverable.coffee.master_key}",
      "subtitle_srt_url": "${deliverable.coffee.srt_key}",
      "subtitle_mode": "soft",
      "voice_volume": 0.9,
      "resolution": {
        "width": 1920,
        "height": 1080
      },
      "subtitle_style": {
        "font_name": "拼搏体",
        "font_scale": 0.9,
        "y_offset": 1
      },
      "bgm": {
        "url": "${audio_key:14}",
        "bgm_volume": 0.08
      },
      "reuse_intermediates": false,
      "cleanup_scratch_on_success": true,
      "segments": [
        {
          "index": 1,
          "text": "推开木门，烘豆机的暖香扑面而来，",
          "media": {
            "url": "${image_key:1}",
            "type": "image"
          }
        },
        {
          "index": 2,
          "text": "咖啡师手起杯落，拉花在杯面绽开一朵郁金香。",
          "media": {
            "url": "${image_key:2}",
            "type": "image"
          }
        }
      ]
    }
  }
]
"""
)


def run_case(case: dict[str, Any], verbose: bool) -> dict[str, Any]:
    iface = case["interface"]
    case_id = case["id"]
    name = case["name"]
    expect = case["expect"]
    path = case["path"]
    poll_tpl = case["poll_path"]
    body = resolve_body(deepcopy(case["body"]))
    kind = expect_kind(expect)
    cfg = POLL[iface]
    meta = INTERFACE_META.get(iface, {})

    t0 = time.perf_counter()
    out: dict[str, Any] = {
        "interface": iface,
        "interface_label": INTERFACE_LABELS.get(iface, iface),
        "case_id": case_id,
        "name": name,
        "expect": expect,
        "passed": False,
        "detail": "",
        "error": None,
        "duration_sec": 0.0,
        # --- 与 e2e 报告一致：按接口记录 请求 → 提交响应 → 轮询结果 ---
        "method": meta.get("method", "POST"),
        "path": path,
        "request_body": body,
        "submit_http_status": None,
        "submit_request_id": None,
        "submit_response": None,
        "poll_method": meta.get("poll_method", "GET"),
        "poll_path": None,
        "poll_http_status": None,
        "poll_request_id": None,
        "poll_result": None,
    }

    try:
        http_status, payload, headers = api_request("POST", path, body, timeout=cfg["submit_timeout"])
        out["submit_http_status"] = http_status
        out["submit_request_id"] = headers.get("x-request-id")
        out["submit_response"] = payload.get("data") if payload.get("data") is not None else payload

        if kind == "validation":
            code = payload.get("code")
            if code == 40001 and http_status in (400, 422):
                out["passed"] = True
                out["detail"] = payload.get("message") or "validation_error"
            else:
                out["detail"] = f"expected validation error, got HTTP {http_status} code={code}"
            return out

        if http_status != 200 or payload.get("code") != 0:
            out["detail"] = f"submit failed: HTTP {http_status}"
            return out

        task_id = (payload.get("data") or {}).get("task_id")
        if not task_id:
            out["detail"] = "missing task_id"
            return out
        if verbose:
            print(f"      task_id={task_id}")

        poll_path = poll_tpl.format(task_id=task_id)
        out["poll_path"] = poll_path
        p_status, p_payload, p_headers = poll_until_done(poll_path, iface, verbose)
        out["poll_http_status"] = p_status
        out["poll_request_id"] = p_headers.get("x-request-id")
        out["poll_result"] = p_payload.get("data")

        polled = out["poll_result"] or {}
        final_status = polled.get("status")

        if kind == "done":
            if final_status == "done":
                result = polled.get("result") or {}
                assert_err = check_poll_result(iface, result, case.get("assert_result"))
                if assert_err:
                    out["detail"] = assert_err
                else:
                    out["passed"] = True
                    out["detail"] = "status=done"
            else:
                out["detail"] = f"expected done, got {final_status!r}"
        elif kind == "poll_failed":
            if final_status == "failed":
                out["passed"] = True
                err = polled.get("error") or {}
                out["detail"] = f"status=failed {err.get('category')} {err.get('message')}"
            else:
                out["detail"] = f"expected failed, got {final_status!r}"

    except Exception as exc:
        out["error"] = str(exc)
        out["detail"] = str(exc)
    finally:
        out["duration_sec"] = round(time.perf_counter() - t0, 3)

    return out


def collect_cases(
    *,
    interfaces: list[str] | None,
    skip_compose: bool,
    case_id: str | None,
    expect: str | None,
) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = list(TEST_CASES)

    if interfaces:
        allowed = set(interfaces)
        cases = [c for c in cases if c.get("interface") in allowed]
    if skip_compose:
        cases = [c for c in cases if c.get("interface") != "compose"]
    if case_id:
        pat = re.compile(case_id)
        cases = [c for c in cases if pat.search(c["id"])]
    if expect == "validation":
        cases = [c for c in cases if c.get("expect") == EXPECT_VALIDATION]
    elif expect == "success":
        cases = [c for c in cases if c.get("expect") == EXPECT_DONE]
    elif expect == "poll_failed":
        cases = [c for c in cases if c.get("expect") == EXPECT_POLL_FAILED]
    return cases


def _result_highlight(r: dict[str, Any]) -> str:
    poll = r.get("poll_result") or {}
    result = poll.get("result")
    if result:
        summary = summarize_result(r["interface"], result)
        parts = []
        for k, v in summary.items():
            if v is None:
                continue
            if isinstance(v, dict):
                parts.append(f"{k}={json.dumps(v, ensure_ascii=False)}")
            else:
                parts.append(f"{k}={v}")
        if parts:
            return " · ".join(parts[:4])
    err = poll.get("error")
    if err:
        return f"{err.get('category', '')} {err.get('message', '')}".strip()
    sub = r.get("submit_response") or {}
    if isinstance(sub, dict) and sub.get("errors"):
        errs = sub["errors"]
        return errs[0].get("msg", "validation_error") if errs else r.get("detail", "")
    if r.get("submit_http_status") is not None:
        return f"HTTP {r['submit_http_status']}"
    return r.get("detail") or ""


def build_interfaces_report(results: list[dict[str, Any]]) -> dict[str, Any]:
    """按接口分组，每条用例为 请求 → submit_response → poll_result（同 e2e 报告）。"""
    grouped: dict[str, Any] = {}
    for iface in ["split", "audio_process", "visual_preview", "compose"]:
        iface_results = [r for r in results if r["interface"] == iface]
        if not iface_results:
            continue
        meta = INTERFACE_META[iface]
        passed = sum(1 for r in iface_results if r["passed"])
        grouped[iface] = {
            "label": meta["label"],
            "method": meta["method"],
            "path": meta["path"],
            "poll_method": meta["poll_method"],
            "poll_path_template": meta["poll_path_template"],
            "summary": {
                "total": len(iface_results),
                "passed": passed,
                "failed": len(iface_results) - passed,
                "pass_rate_pct": round(100 * passed / len(iface_results), 1),
            },
            "cases": [
                {
                    "case_id": r["case_id"],
                    "name": r["name"],
                    "expect": r["expect"],
                    "passed": r["passed"],
                    "duration_sec": r["duration_sec"],
                    "detail": r.get("detail"),
                    "error": r.get("error"),
                    "method": r["method"],
                    "path": r["path"],
                    "request_body": r["request_body"],
                    "submit_http_status": r["submit_http_status"],
                    "submit_request_id": r["submit_request_id"],
                    "submit_response": r["submit_response"],
                    "poll_method": r["poll_method"],
                    "poll_path": r["poll_path"],
                    "poll_http_status": r["poll_http_status"],
                    "poll_request_id": r["poll_request_id"],
                    "poll_result": r["poll_result"],
                }
                for r in iface_results
            ],
        }
    return grouped


def build_statistics(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    failed = total - passed
    duration = sum(r.get("duration_sec", 0) for r in results)

    by_interface: dict[str, dict[str, Any]] = {}
    for r in results:
        iface = r["interface"]
        row = by_interface.setdefault(
            iface,
            {
                "label": INTERFACE_LABELS.get(iface, iface),
                "total": 0,
                "passed": 0,
                "failed": 0,
                "duration_sec": 0.0,
            },
        )
        row["total"] += 1
        row["passed"] += 1 if r["passed"] else 0
        row["failed"] += 0 if r["passed"] else 1
        row["duration_sec"] = round(row["duration_sec"] + r.get("duration_sec", 0), 3)

    by_expect: dict[str, dict[str, Any]] = {}
    for r in results:
        exp = r["expect"]
        row = by_expect.setdefault(
            exp,
            {
                "label": EXPECT_LABELS.get(exp, exp),
                "total": 0,
                "passed": 0,
                "failed": 0,
            },
        )
        row["total"] += 1
        row["passed"] += 1 if r["passed"] else 0
        row["failed"] += 0 if r["passed"] else 1

    for row in by_interface.values():
        row["pass_rate_pct"] = round(100 * row["passed"] / row["total"], 1) if row["total"] else 0
    for row in by_expect.values():
        row["pass_rate_pct"] = round(100 * row["passed"] / row["total"], 1) if row["total"] else 0

    failed_cases = [
        {
            "interface": r["interface"],
            "interface_label": INTERFACE_LABELS.get(r["interface"], r["interface"]),
            "case_id": r["case_id"],
            "name": r["name"],
            "detail": r.get("detail"),
            "error": r.get("error"),
            "duration_sec": r.get("duration_sec"),
        }
        for r in results
        if not r["passed"]
    ]

    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "pass_rate_pct": round(100 * passed / total, 1) if total else 0,
        "duration_sec": round(duration, 1),
        "by_interface": by_interface,
        "by_expect": by_expect,
        "failed_cases": failed_cases,
    }


def print_result_line(r: dict[str, Any], *, index: int, total: int, verbose: bool) -> None:
    icon = "✓" if r["passed"] else "✗"
    label = INTERFACE_LABELS.get(r["interface"], r["interface"])
    highlight = _result_highlight(r)
    dur = r.get("duration_sec", 0)
    print(
        f"  {icon} ({index}/{total}) [{label}] {r['case_id']}  {r['name']}  ({dur}s)",
        flush=True,
    )
    if highlight:
        print(f"      → {highlight}", flush=True)
    if verbose or not r["passed"]:
        print_result_detail(r, verbose)


def print_result_detail(r: dict[str, Any], verbose: bool) -> None:
    if r.get("error"):
        print(f"      error: {r['error']}", flush=True)
    print(
        f"      submit: HTTP {r.get('submit_http_status')} "
        f"request_id={r.get('submit_request_id')}",
        flush=True,
    )
    if r.get("submit_response") is not None and (verbose or not r["passed"]):
        print(json.dumps(r["submit_response"], ensure_ascii=False, indent=2), flush=True)
    if r.get("poll_path"):
        print(
            f"      poll:   HTTP {r.get('poll_http_status')} "
            f"{r['poll_path']} request_id={r.get('poll_request_id')}",
            flush=True,
        )
        if r.get("poll_result") is not None and (verbose or not r["passed"]):
            print(json.dumps(r["poll_result"], ensure_ascii=False, indent=2), flush=True)


def print_summary_console(
    stats: dict[str, Any],
    *,
    started_at: str,
    finished_at: str,
    report_path: Path,
) -> None:
    w = 56
    print()
    print("=" * w)
    print("  Flow B API 测试报告")
    print("=" * w)
    print(f"  开始: {started_at}")
    print(f"  结束: {finished_at}")
    print(f"  API:  {API_BASE}")
    print(f"  OSS:  {OSS_HTTPS_BASE}/{OSS_TEST_ROOT}/")
    print()
    print(
        f"  总计 {stats['total']} 条  ·  "
        f"通过 {stats['passed']}  ·  "
        f"失败 {stats['failed']}  ·  "
        f"通过率 {stats['pass_rate_pct']}%  ·  "
        f"耗时 {stats['duration_sec']}s"
    )
    print()
    print("  按接口:")
    iface_order = ["split", "audio_process", "visual_preview", "compose"]
    for iface in iface_order:
        row = stats["by_interface"].get(iface)
        if not row:
            continue
        bar = "█" * int(row["pass_rate_pct"] / 10) + "░" * (10 - int(row["pass_rate_pct"] / 10))
        print(
            f"    {row['label']:<6} {row['passed']:>2}/{row['total']:<2}  "
            f"{row['pass_rate_pct']:>5.1f}%  {bar}  ({row['duration_sec']}s)"
        )
    print()
    print("  按类型:")
    for row in stats["by_expect"].values():
        print(f"    {row['label']:<8} {row['passed']}/{row['total']}  ({row['pass_rate_pct']}%)")
    if stats["failed_cases"]:
        print()
        print("  失败用例:")
        for fc in stats["failed_cases"]:
            print(f"    ✗ [{fc['interface_label']}] {fc['case_id']}")
            print(f"      {fc['name']}")
            print(f"      原因: {fc['detail']}")
    print()
    print(f"  报告: {report_path}")
    print("=" * w)


def render_markdown_report(report: dict[str, Any]) -> str:
    stats = report["summary"]
    lines = [
        "# Flow B API 测试报告",
        "",
        f"- **开始时间**: {report.get('started_at', report.get('run_at', ''))}",
        f"- **结束时间**: {report.get('finished_at', '')}",
        f"- **API**: `{report.get('api_base', '')}`",
        f"- **OSS**: `{report.get('oss_https_base', '')}/{report.get('oss_test_root', '')}/`",
        "",
        "## 总览",
        "",
        "| 指标 | 数值 |",
        "|------|------|",
        f"| 用例总数 | {stats['total']} |",
        f"| 通过 | {stats['passed']} |",
        f"| 失败 | {stats['failed']} |",
        f"| 通过率 | {stats['pass_rate_pct']}% |",
        f"| 总耗时 | {stats['duration_sec']}s |",
        "",
        "## 按接口统计",
        "",
        "| 接口 | 通过/总数 | 通过率 |",
        "|------|-----------|--------|",
    ]
    for iface in ["split", "audio_process", "visual_preview", "compose"]:
        block = report.get("interfaces", {}).get(iface)
        if not block:
            continue
        s = block["summary"]
        lines.append(f"| {block['label']} | {s['passed']}/{s['total']} | {s['pass_rate_pct']}% |")

    if stats["failed_cases"]:
        lines.extend(["", "## 失败用例", ""])
        for fc in stats["failed_cases"]:
            lines.append(f"- ✗ **{fc['case_id']}** ({fc['interface_label']}): {fc['name']} — {fc['detail']}")

    lines.extend(["", "---", "", "## 按接口 · 请求与响应", ""])

    for iface in ["split", "audio_process", "visual_preview", "compose"]:
        block = report.get("interfaces", {}).get(iface)
        if not block:
            continue
        s = block["summary"]
        lines.append(f"### {block['label']} `{iface}` — {s['passed']}/{s['total']} 通过")
        lines.append("")
        lines.append(f"- **提交**: `{block['method']} {block['path']}`")
        lines.append(f"- **轮询**: `{block['poll_method']} {block['poll_path_template']}`")
        lines.append("")

        for c in block["cases"]:
            mark = "✓" if c["passed"] else "✗"
            lines.append(f"#### {mark} `{c['case_id']}` — {c['name']}")
            lines.append("")
            lines.append(f"**{c['method']}** `{c['path']}`")
            lines.append("")
            lines.append("请求 `request_body`:")
            lines.append("```json")
            lines.append(json.dumps(c["request_body"], ensure_ascii=False, indent=2))
            lines.append("```")
            lines.append("")
            lines.append(f"提交响应 `submit_response` (HTTP {c['submit_http_status']}):")
            lines.append("```json")
            lines.append(json.dumps(c["submit_response"], ensure_ascii=False, indent=2))
            lines.append("```")
            if c.get("poll_path"):
                lines.append("")
                lines.append(f"**{c['poll_method']}** `{c['poll_path']}`")
                lines.append("")
                lines.append(f"轮询结果 `poll_result` (HTTP {c['poll_http_status']}):")
                lines.append("```json")
                lines.append(json.dumps(c["poll_result"], ensure_ascii=False, indent=2))
                lines.append("```")
            lines.append("")

    lines.append("")
    return "\n".join(lines)


def print_result(r: dict[str, Any], verbose: bool) -> None:
    """保留兼容；请用 print_result_line。"""
    print_result_detail(r, verbose)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Flow B 四接口 API 自动化测试（分句/配音/预览/合成）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s --skip-compose              跑除合成外的全部用例
  %(prog)s --interface split           只测分句
  %(prog)s --expect success              只测成功路径用例（当前默认即全部为 success）
  %(prog)s --case-id compose_01 -v         联调单条合成用例
        """.strip(),
    )
    parser.add_argument(
        "--interface",
        action="append",
        dest="interfaces",
        choices=["split", "audio_process", "visual_preview", "compose"],
        metavar="NAME",
        help="只跑指定接口：split | audio_process | visual_preview | compose（可重复）",
    )
    parser.add_argument(
        "--case-id",
        metavar="REGEX",
        help="用例 id 正则过滤，例如 split_01 或 compose_03",
    )
    parser.add_argument(
        "--expect",
        choices=["validation", "success", "poll_failed"],
        help="期望类型过滤（当前用例均为 success）",
    )
    parser.add_argument(
        "--skip-compose",
        action="store_true",
        default=SKIP_COMPOSE_DEFAULT,
        help="跳过视频合成 compose（耗时长，首次联调建议开启）",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="输出完整 submit_response / poll_result JSON",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="首个用例失败后立即退出",
    )
    args = parser.parse_args()

    # health
    try:
        st, pl, _ = api_request("GET", "/api/v1/health", timeout=30)
        if st != 200 or pl.get("code") != 0:
            print(f"health check failed: {st} {pl}", file=sys.stderr)
            return 2
    except Exception as exc:
        print(f"health check failed: {exc}", file=sys.stderr)
        return 2

    cases = collect_cases(
        interfaces=args.interfaces,
        skip_compose=args.skip_compose,
        case_id=args.case_id,
        expect=args.expect,
    )

    if not cases:
        print("no cases matched", file=sys.stderr)
        return 2

    root = Path(__file__).resolve().parents[1]
    report_path = root / REPORT_FILE

    started_at = datetime.now(timezone.utc).isoformat()
    print(f"API: {API_BASE}")
    print(f"OSS: {OSS_HTTPS_BASE}/{OSS_TEST_ROOT}/")
    print(f"用例: {len(cases)} 条")
    print()

    results = []
    for i, case in enumerate(cases, 1):
        r = run_case(case, args.verbose)
        results.append(r)
        print_result_line(r, index=i, total=len(cases), verbose=args.verbose)
        if args.fail_fast and not r["passed"]:
            break

    finished_at = datetime.now(timezone.utc).isoformat()
    stats = build_statistics(results)
    interfaces = build_interfaces_report(results)

    report = {
        "run_at": started_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "api_base": API_BASE,
        "oss_https_base": OSS_HTTPS_BASE,
        "oss_test_root": OSS_TEST_ROOT,
        "summary": stats,
        "interfaces": interfaces,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_markdown_report(report) + "\n", encoding="utf-8")

    print_summary_console(
        stats,
        started_at=started_at,
        finished_at=finished_at,
        report_path=report_path,
    )
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130)
