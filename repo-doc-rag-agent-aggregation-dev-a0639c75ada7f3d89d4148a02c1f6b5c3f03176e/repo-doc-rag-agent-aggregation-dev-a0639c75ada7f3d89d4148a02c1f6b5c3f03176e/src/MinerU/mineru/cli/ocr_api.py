# Copyright (c) Opendatalab. All rights reserved.
import base64
import os
import threading
import time
from collections import OrderedDict
from typing import Any

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

try:
    import torch
except Exception:  # pragma: no cover - torch is required in the Docker image.
    torch = None

from mineru.model.ocr.pytorch_paddle import PytorchPaddleOCR
from mineru.utils.config_reader import get_device
from mineru.utils.ocr_language import normalize_ocr_model_lang


DEFAULT_LANG = os.getenv("MINERU_OCR_LANG", "ch")
MAX_LANG_MODELS = max(1, int(os.getenv("MINERU_OCR_MAX_LANG_MODELS", "1")))

app = FastAPI(title="MinerU OCR CPU API", version="0.1.0")

_models: OrderedDict[str, PytorchPaddleOCR] = OrderedDict()
_model_lock = threading.Lock()
_ocr_lock = threading.Lock()


class OCRRequest(BaseModel):
    image_base64: str | None = Field(default=None, description="Base64 encoded image bytes. Data URLs are accepted.")
    image_path: str | None = Field(default=None, description="Path inside the OCR container.")
    resolved_path: str | None = Field(default=None, description="Alias accepted from the HTML adapter payload.")
    lang: str = Field(default=DEFAULT_LANG, description="MinerU OCR language, e.g. ch, en, latin.")
    det: bool = Field(default=True, description="Run text detection.")
    rec: bool = Field(default=True, description="Run text recognition.")


def _configure_threads() -> None:
    if torch is None:
        return
    intra_threads = int(os.getenv("MINERU_INTRA_OP_NUM_THREADS", "4"))
    inter_threads = int(os.getenv("MINERU_INTER_OP_NUM_THREADS", "1"))
    try:
        torch.set_num_threads(intra_threads)
        torch.set_num_interop_threads(inter_threads)
    except RuntimeError:
        # PyTorch only allows changing inter-op threads before parallel work starts.
        pass


def _decode_base64_image(value: str) -> np.ndarray:
    if "," in value and value.lstrip().lower().startswith("data:"):
        value = value.split(",", 1)[1]
    try:
        data = base64.b64decode(value, validate=False)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid image_base64: {exc}") from exc
    return _decode_image_bytes(data)


def _decode_image_bytes(data: bytes) -> np.ndarray:
    buffer = np.frombuffer(data, dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if image is not None:
        return image

    try:
        from PIL import Image
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Unsupported image bytes") from exc

    try:
        import io

        with Image.open(io.BytesIO(data)) as pil_image:
            rgb = pil_image.convert("RGB")
            return cv2.cvtColor(np.array(rgb), cv2.COLOR_RGB2BGR)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Unsupported image bytes: {exc}") from exc


def _load_image(request: OCRRequest) -> tuple[np.ndarray, str]:
    if request.image_base64:
        return _decode_base64_image(request.image_base64), "base64"

    image_path = request.image_path or request.resolved_path
    if not image_path:
        raise HTTPException(status_code=422, detail="Provide image_base64, image_path, or resolved_path.")
    if not os.path.exists(image_path):
        raise HTTPException(status_code=404, detail=f"Image path not found inside OCR container: {image_path}")

    image = cv2.imread(image_path, cv2.IMREAD_COLOR)
    if image is None:
        raise HTTPException(status_code=400, detail=f"Failed to decode image path: {image_path}")
    return image, "path"


def _get_model(lang: str) -> tuple[str, PytorchPaddleOCR]:
    normalized_lang = normalize_ocr_model_lang(lang)
    with _model_lock:
        model = _models.get(normalized_lang)
        if model is not None:
            _models.move_to_end(normalized_lang)
            return normalized_lang, model

        model = PytorchPaddleOCR(lang=normalized_lang)
        _models[normalized_lang] = model
        _models.move_to_end(normalized_lang)
        while len(_models) > MAX_LANG_MODELS:
            _models.popitem(last=False)
        return normalized_lang, model


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_ocr_result(raw_result: Any, det: bool, rec: bool) -> tuple[str, list[dict[str, Any]]]:
    sample = raw_result[0] if isinstance(raw_result, list) and len(raw_result) == 1 else raw_result
    if not sample:
        return "", []

    items: list[dict[str, Any]] = []
    texts: list[str] = []
    if det and rec:
        for entry in sample:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                continue
            box, rec_result = entry
            text = ""
            score = None
            if isinstance(rec_result, (list, tuple)) and rec_result:
                text = str(rec_result[0] or "")
                score = _to_float(rec_result[1] if len(rec_result) > 1 else None)
            else:
                text = str(rec_result or "")
            if text:
                texts.append(text)
            items.append({"text": text, "score": score, "box": box})
        return "\n".join(texts), items

    if det and not rec:
        return "", [{"box": box} for box in sample]

    if rec:
        for rec_result in sample:
            text = ""
            score = None
            if isinstance(rec_result, (list, tuple)) and rec_result:
                text = str(rec_result[0] or "")
                score = _to_float(rec_result[1] if len(rec_result) > 1 else None)
            else:
                text = str(rec_result or "")
            if text:
                texts.append(text)
            items.append({"text": text, "score": score, "box": None})
    return "\n".join(texts), items


@app.on_event("startup")
def _startup() -> None:
    _configure_threads()
    if os.getenv("MINERU_OCR_PRELOAD", "").lower() in {"1", "true", "yes", "on"}:
        _get_model(DEFAULT_LANG)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "device": get_device(),
        "default_lang": DEFAULT_LANG,
        "loaded_langs": list(_models.keys()),
        "max_lang_models": MAX_LANG_MODELS,
    }


@app.get("/ready")
def ready(lang: str = DEFAULT_LANG) -> dict[str, Any]:
    started = time.perf_counter()
    normalized_lang, _ = _get_model(lang)
    return {
        "status": "ready",
        "device": get_device(),
        "lang": normalized_lang,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


@app.post("/ocr")
def ocr(request: OCRRequest) -> dict[str, Any]:
    if not request.det and not request.rec:
        raise HTTPException(status_code=422, detail="At least one of det or rec must be true.")

    image, input_kind = _load_image(request)
    normalized_lang, model = _get_model(request.lang)
    started = time.perf_counter()
    with _ocr_lock:
        raw_result = model.ocr(image, det=request.det, rec=request.rec)
    text, items = _normalize_ocr_result(raw_result, request.det, request.rec)
    height, width = image.shape[:2]
    return {
        "text": text,
        "ocr_text": text,
        "items": items,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "lang": normalized_lang,
        "device": get_device(),
        "input": {
            "kind": input_kind,
            "width": int(width),
            "height": int(height),
        },
    }
