from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ServiceConfig:
    # Model settings
    clip_model_path: str
    clip_device: str = "cpu"
    clip_batch_size: int = 16
    bge_model_name: str = "BAAI/bge-large-zh-v1.5"
    bge_model_path: str | None = None  # Optional local path for BGE model
    bge_device: str = "cpu"
    bge_batch_size: int = 16
    bge_local_files_only: bool = False
    auto_download_models: bool = True

    # Media processing settings
    download_timeout: float = 3600.0
    max_image_mb: int = 0  # 0 = no size limit
    max_video_mb: int = 0  # 0 = no size limit
    max_video_resolution: int = 7680  # Max width/height for ffmpeg segment scaling (up to 8K)
    max_frame_resolution: int = 1920  # Max resolution for extracted frames
    media_tmp_dir: str = "runtime/tmp"
    log_dir: str = "runtime/logs"
    tmp_max_age_hours: int = 6
    log_max_age_days: int = 7
    log_file_max_mb: int = 50
    log_file_backup_count: int = 5
    runtime_cleanup_interval_hours: int = 1

    # OSS settings
    aliyun_oss_endpoint: str | None = None
    aliyun_oss_access_key_id: str | None = None
    aliyun_oss_access_key_secret: str | None = None
    aliyun_oss_bucket: str | None = None
    
    # Task management settings
    task_db_path: str = "runtime/tasks.db"
    max_tasks: int = 10000
    task_ttl_hours: int = 24
    
    # Server settings
    service_port: int = 0  # Must be set via EMBEDDING_SERVICE_PORT, 0 means not configured
    service_host: str = "0.0.0.0"
    workers: int = 3
    log_level: str = "INFO"
    embedding_workers: int = 2  # Thread pool size for async tasks

    def oss_configured(self) -> bool:
        return bool(
            self.aliyun_oss_endpoint
            and self.aliyun_oss_access_key_id
            and self.aliyun_oss_access_key_secret
            and self.aliyun_oss_bucket
        )

    def missing_oss_env_names(self) -> list[str]:
        missing: list[str] = []
        if not self.aliyun_oss_endpoint:
            missing.append("ALIYUN_OSS_ENDPOINT")
        if not self.aliyun_oss_access_key_id:
            missing.append("ALIYUN_OSS_ACCESS_KEY_ID")
        if not self.aliyun_oss_access_key_secret:
            missing.append("ALIYUN_OSS_ACCESS_KEY_SECRET")
        if not self.aliyun_oss_bucket:
            missing.append("ALIYUN_OSS_BUCKET")
        return missing

    @classmethod
    def from_env(cls) -> ServiceConfig:
        from .env_loader import load_project_env

        load_project_env()

        # Use relative path "models" from project root
        # This resolves to: chinese-clip-main/models
        default_model = Path(__file__).resolve().parents[1] / "models"
        
        # Ensure the path exists
        if not default_model.exists():
            import logging
            logging.warning(f"Model directory does not exist: {default_model}")
            logging.info(f"Please ensure models are downloaded to: {default_model}")
        
        # Check for local BGE model
        bge_local_path = None
        default_bge_local = default_model / "bge-large-zh-v1.5"
        if default_bge_local.exists():
            bge_local_path = str(default_bge_local)
            import logging
            logging.info(f"Found local BGE model at: {bge_local_path}")
        
        return cls(
            # Model settings
            clip_model_path=os.environ.get("CHINESE_CLIP_MODEL_PATH", str(default_model)),
            clip_device=os.environ.get("CLIP_DEVICE", "cpu"),
            clip_batch_size=int(os.environ.get("CLIP_BATCH_SIZE", "16")),
            bge_model_name=os.environ.get("PICTURE_BGE_MODEL", "BAAI/bge-large-zh-v1.5"),
            bge_model_path=os.environ.get("BGE_MODEL_PATH") or bge_local_path,
            bge_device=os.environ.get("BGE_DEVICE", "cpu"),
            bge_batch_size=int(os.environ.get("BGE_BATCH_SIZE", "16")),
            bge_local_files_only=bool(os.environ.get("HF_LOCAL_FILES_ONLY")),
            auto_download_models=os.environ.get("AUTO_DOWNLOAD_MODELS", "1").strip().lower()
            not in {"0", "false", "no", "off"},
            
            # Media processing settings
            download_timeout=float(os.environ.get("MEDIA_DOWNLOAD_TIMEOUT", "3600")),
            max_image_mb=int(os.environ.get("MEDIA_MAX_IMAGE_MB", "0")),
            max_video_mb=int(os.environ.get("MEDIA_MAX_VIDEO_MB", "0")),
            max_video_resolution=int(os.environ.get("MAX_VIDEO_RESOLUTION", "7680")),
            max_frame_resolution=int(os.environ.get("MAX_FRAME_RESOLUTION", "1920")),
            media_tmp_dir=os.environ.get("MEDIA_TMP_DIR", "runtime/tmp"),
            log_dir=os.environ.get("LOG_DIR", "runtime/logs"),
            tmp_max_age_hours=int(os.environ.get("TMP_MAX_AGE_HOURS", "6")),
            log_max_age_days=int(os.environ.get("LOG_MAX_AGE_DAYS", "7")),
            log_file_max_mb=int(os.environ.get("LOG_FILE_MAX_MB", "50")),
            log_file_backup_count=int(os.environ.get("LOG_FILE_BACKUP_COUNT", "5")),
            runtime_cleanup_interval_hours=int(os.environ.get("RUNTIME_CLEANUP_INTERVAL_HOURS", "1")),

            # OSS settings
            aliyun_oss_endpoint=os.environ.get("ALIYUN_OSS_ENDPOINT") or None,
            aliyun_oss_access_key_id=os.environ.get("ALIYUN_OSS_ACCESS_KEY_ID") or None,
            aliyun_oss_access_key_secret=os.environ.get("ALIYUN_OSS_ACCESS_KEY_SECRET") or None,
            aliyun_oss_bucket=os.environ.get("ALIYUN_OSS_BUCKET") or None,
            
            # Task management settings
            task_db_path=os.environ.get("TASK_DB_PATH", "runtime/tasks.db"),
            max_tasks=int(os.environ.get("MAX_TASKS", "10000")),
            task_ttl_hours=int(os.environ.get("TASK_TTL_HOURS", "24")),
            
            # Server settings
            service_port=int(os.environ.get("EMBEDDING_SERVICE_PORT", "0")),  # 0 means not configured
            service_host=os.environ.get("EMBEDDING_SERVICE_HOST", "0.0.0.0"),
            workers=int(os.environ.get("WORKERS", "3")),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            embedding_workers=int(os.environ.get("EMBEDDING_WORKERS", "2")),
        )

    def validate(self) -> None:
        """Validate configuration values."""
        # Port must be specified
        if self.service_port == 0:
            raise ValueError(
                "EMBEDDING_SERVICE_PORT environment variable is required.\n"
                "Please set it before starting the service:\n"
                "  Windows: $env:EMBEDDING_SERVICE_PORT=\"8030\"\n"
                "  Linux/Mac: export EMBEDDING_SERVICE_PORT=8030"
            )
        if not 1 <= self.service_port <= 65535:
            raise ValueError(f"Invalid port: {self.service_port}")
        if self.clip_batch_size < 1:
            raise ValueError(f"Invalid clip_batch_size: {self.clip_batch_size}")
        if self.bge_batch_size < 1:
            raise ValueError(f"Invalid bge_batch_size: {self.bge_batch_size}")
        if self.max_tasks < 1:
            raise ValueError(f"Invalid max_tasks: {self.max_tasks}")
        if self.task_ttl_hours < 1:
            raise ValueError(f"Invalid task_ttl_hours: {self.task_ttl_hours}")
        if self.tmp_max_age_hours < 1:
            raise ValueError(f"Invalid tmp_max_age_hours: {self.tmp_max_age_hours}")
        if self.log_max_age_days < 1:
            raise ValueError(f"Invalid log_max_age_days: {self.log_max_age_days}")
        if self.log_file_max_mb < 1:
            raise ValueError(f"Invalid log_file_max_mb: {self.log_file_max_mb}")
        if self.log_file_backup_count < 0:
            raise ValueError(f"Invalid log_file_backup_count: {self.log_file_backup_count}")
        if self.runtime_cleanup_interval_hours < 1:
            raise ValueError(f"Invalid runtime_cleanup_interval_hours: {self.runtime_cleanup_interval_hours}")
        if self.download_timeout <= 0:
            raise ValueError(f"Invalid download_timeout: {self.download_timeout}")
        if self.max_image_mb < 0:
            raise ValueError(f"Invalid max_image_mb: {self.max_image_mb} (0 = unlimited)")
        if self.max_video_mb < 0:
            raise ValueError(f"Invalid max_video_mb: {self.max_video_mb} (0 = unlimited)")

        from .model_bootstrap import bge_model_ready, clip_model_ready, resolve_bge_model_dir

        clip_dir = Path(self.clip_model_path)
        if not clip_model_ready(clip_dir):
            raise ValueError(
                f"CLIP model is not ready at {self.clip_model_path}. "
                "Enable AUTO_DOWNLOAD_MODELS (default) or run: python scripts/download_models.py"
            )
        bge_dir = resolve_bge_model_dir(self)
        if self.bge_local_files_only or self.bge_model_path or self.auto_download_models:
            if not bge_model_ready(bge_dir):
                raise ValueError(
                    f"BGE model is not ready at {bge_dir}. "
                    "Enable AUTO_DOWNLOAD_MODELS (default) or download BGE to that directory."
                )
