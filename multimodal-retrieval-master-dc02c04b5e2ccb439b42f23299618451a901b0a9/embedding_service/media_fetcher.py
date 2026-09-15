from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from io import BytesIO

from PIL import Image
from requests.exceptions import ConnectionError as RequestsConnectionError, ReadTimeout

from .config import ServiceConfig
from .errors import MediaDownloadError, ValidationError
from .user_errors import (
    REASON_EMPTY_FILE,
    REASON_FILE_TOO_LARGE,
    REASON_DOWNLOAD_TIMEOUT,
    REASON_IMAGE_LOAD_FAILED,
    REASON_OSS_DOWNLOAD_FAILED,
    REASON_OSS_NOT_CONFIGURED,
)
from .oss_client import create_oss_bucket
from .schemas import OssMediaRef

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoadedImage:
    image: Image.Image
    source: str
    content_type: str
    bucket: str | None
    object_key: str


def _download_oss_object(ref: OssMediaRef, config: ServiceConfig) -> bytes:
    """Download object from Aliyun OSS."""
    try:
        import oss2
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise MediaDownloadError(
            "oss2 is required for object_key access: pip install oss2",
            details={"reason": REASON_OSS_NOT_CONFIGURED},
        ) from exc

    try:
        bucket = create_oss_bucket(config)
    except ValueError as exc:
        raise ValidationError(str(exc), details={"reason": REASON_OSS_NOT_CONFIGURED}) from exc
    bucket_name = config.aliyun_oss_bucket
    assert bucket_name is not None

    max_attempts = 5
    base_delay = 1.0
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = bucket.get_object(ref.object_key)
            data = result.read()
            max_bytes = config.max_image_mb * 1024 * 1024
            if max_bytes > 0 and len(data) > max_bytes:
                raise MediaDownloadError(
                    f"Media exceeds max size: {len(data)} > {max_bytes} bytes",
                    details={
                        "reason": REASON_FILE_TOO_LARGE,
                        "bucket": bucket_name,
                        "object_key": ref.object_key,
                        "size_bytes": len(data),
                        "max_bytes": max_bytes,
                        "max_image_mb": config.max_image_mb,
                    },
                )
            if len(data) == 0:
                raise MediaDownloadError(
                    f"OSS object is empty: {ref.object_key}",
                    details={
                        "reason": REASON_EMPTY_FILE,
                        "bucket": bucket_name,
                        "object_key": ref.object_key,
                        "size_bytes": 0,
                    },
                )
            logger.info(f"Downloaded {len(data)} bytes from OSS: {bucket_name}/{ref.object_key}")
            return data
        except (ReadTimeout, RequestsConnectionError, TimeoutError) as exc:
            last_exc = exc
            if attempt >= max_attempts:
                break
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                "OSS download timed out for %s (attempt %s/%s, timeout=%.1fs); retrying in %.1fs",
                ref.object_key,
                attempt,
                max_attempts,
                config.download_timeout,
                delay,
            )
            time.sleep(delay)
        except oss2.exceptions.OssError as exc:
            raise MediaDownloadError(
                f"Failed to download from OSS: {exc}",
                details={
                    "reason": REASON_OSS_DOWNLOAD_FAILED,
                    "bucket": bucket_name,
                    "object_key": ref.object_key,
                },
            ) from exc

    assert last_exc is not None
    raise MediaDownloadError(
        f"Failed to download from OSS after {max_attempts} attempts: {last_exc}",
        details={
            "reason": REASON_DOWNLOAD_TIMEOUT,
            "bucket": bucket_name,
            "object_key": ref.object_key,
            "attempts": max_attempts,
            "timeout_seconds": config.download_timeout,
        },
    ) from last_exc


def load_image(ref: OssMediaRef, config: ServiceConfig) -> LoadedImage:
    """Load image from OSS by object_key."""
    try:
        data = _download_oss_object(ref, config)
        image = Image.open(BytesIO(data)).convert("RGB")
        logger.debug(f"Loaded image: {image.size}")
        return LoadedImage(
            image=image,
            source="oss",
            content_type=ref.content_type,
            bucket=config.aliyun_oss_bucket,
            object_key=ref.object_key,
        )
    except (MediaDownloadError, ValidationError):
        raise
    except Exception as exc:
        raise MediaDownloadError(
            f"Failed to load image: {exc}",
            details={"reason": REASON_IMAGE_LOAD_FAILED, "object_key": ref.object_key},
        ) from exc
