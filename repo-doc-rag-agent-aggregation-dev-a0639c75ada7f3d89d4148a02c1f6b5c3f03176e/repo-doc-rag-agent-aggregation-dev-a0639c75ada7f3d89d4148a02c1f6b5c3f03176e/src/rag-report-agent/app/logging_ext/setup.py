import logging
import os
from logging.handlers import TimedRotatingFileHandler

from app.config import settings
from app.logging_ext.oss_handler import OSSUploadHandler


def setup_logging():
    os.makedirs("logs", exist_ok=True)
    root = logging.getLogger()
    root.setLevel(settings.log_level)
    root.handlers.clear()

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = TimedRotatingFileHandler(
        "logs/app.log",
        when="midnight",
        backupCount=30,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if settings.oss_enabled:
        oss_handler = OSSUploadHandler(
            endpoint=settings.oss_endpoint,
            access_key=settings.oss_access_key,
            secret_key=settings.oss_secret_key,
            bucket=settings.oss_bucket,
            prefix=settings.oss_log_prefix,
        )
        oss_handler.setFormatter(formatter)
        oss_handler.setLevel(logging.INFO)
        root.addHandler(oss_handler)
