from __future__ import annotations

from .config import ServiceConfig


def create_oss_bucket(config: ServiceConfig):
    """Create an oss2 Bucket with MEDIA_DOWNLOAD_TIMEOUT applied as request timeout."""
    import oss2

    if not config.oss_configured():
        missing = ", ".join(config.missing_oss_env_names())
        raise ValueError(
            f"OSS is not configured ({missing}). "
            "Set variables in .env or environment and restart the service."
        )

    bucket_name = config.aliyun_oss_bucket
    assert bucket_name is not None

    auth = oss2.Auth(config.aliyun_oss_access_key_id, config.aliyun_oss_access_key_secret)
    return oss2.Bucket(
        auth,
        config.aliyun_oss_endpoint,
        bucket_name,
        connect_timeout=config.download_timeout,
    )
