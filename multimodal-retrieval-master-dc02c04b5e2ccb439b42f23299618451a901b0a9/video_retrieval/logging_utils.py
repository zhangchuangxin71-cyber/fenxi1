from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_logger(logs_dir: Path, name: str = "video_retrieval") -> logging.Logger:
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    file_handler = logging.FileHandler(logs_dir / "pipeline.log", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


def append_error_log(logs_dir: Path, payload: dict) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    data = {"timestamp": utc_now_iso(), **payload}
    with (logs_dir / "errors.jsonl").open("a", encoding="utf-8") as writer:
        writer.write(json.dumps(data, ensure_ascii=False) + "\n")
