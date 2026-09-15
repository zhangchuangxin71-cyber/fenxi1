import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """轻量加载 .env（不覆盖已有环境变量）。"""
    if not path.exists():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


_load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
WORK_DIR = DATA_DIR / "work"  # 兼容旧缓存；OSS-native 主链路不存完整媒体
APP_VERSION = os.environ.get("APP_VERSION", "1.0.0")
# 镜像构建时注入；本地 uvicorn 未设置则为 unknown
APP_GIT_SHA = os.environ.get("APP_GIT_SHA", "").strip() or "unknown"
APP_BUILD_TIME = os.environ.get("APP_BUILD_TIME", "").strip() or "unknown"

# 逗号分隔的前端 Origin 白名单；开发默认放行本地 Vite
_cors_raw = os.environ.get(
    "CORS_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173",
).strip()
CORS_ORIGINS = [o.strip() for o in _cors_raw.split(",") if o.strip()] or [
    "http://localhost:5173"
]

def _normalize_public_base(raw: str) -> str:
    """规范化对外 Origin。常见误写 API_PUBLIC_BASE==http://... 会多出一个前导 '='。"""
    v = (raw or "").strip().strip("'").strip('"').rstrip("/")
    while v.startswith("="):
        v = v[1:].lstrip()
    return v


# 对外绝对 URL 前缀（preview_url 等）；留空则按当前请求的 Host 拼
API_PUBLIC_BASE = _normalize_public_base(os.environ.get("API_PUBLIC_BASE", ""))

# OSS 公网 CDN 前缀（传 oss_key 导入时：preview_url = {OSS_CDN_BASE}/{oss_key}）
# 传 url 导入时：preview_url 直接返回该 url，不走 CDN 拼接
# 例：https://cdn.example.com + key=dev/a.mp4
#   → https://cdn.example.com/dev/a.mp4
OSS_CDN_BASE = _normalize_public_base(os.environ.get("OSS_CDN_BASE", ""))

ALLOWED_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".m4v"}
MAX_UPLOAD_SIZE = 2 * 1024 * 1024 * 1024  # 2GB

# 阿里云 OSS（oss_key、URL 流式转存、staging 与成品发布均依赖）
OSS_ACCESS_KEY_ID = os.environ.get("OSS_ACCESS_KEY_ID", "").strip()
OSS_ACCESS_KEY_SECRET = os.environ.get("OSS_ACCESS_KEY_SECRET", "").strip()
OSS_ENDPOINT = os.environ.get("OSS_ENDPOINT", "").strip().rstrip("/")
OSS_BUCKET = os.environ.get("OSS_BUCKET", "").strip()
OSS_PREFIX = os.environ.get("OSS_PREFIX", "video-clip/").strip()
if OSS_PREFIX and not OSS_PREFIX.endswith("/"):
    OSS_PREFIX += "/"
_oss_flag = os.environ.get("OSS_ENABLED", "").strip().lower()
if _oss_flag in ("0", "false", "no", "off"):
    OSS_ENABLED = False
elif _oss_flag in ("1", "true", "yes", "on"):
    OSS_ENABLED = True
else:
    OSS_ENABLED = bool(
        OSS_ACCESS_KEY_ID
        and OSS_ACCESS_KEY_SECRET
        and OSS_ENDPOINT
        and OSS_BUCKET
    )
OSS_SIGN_EXPIRES = int(os.environ.get("OSS_SIGN_EXPIRES", "3600"))
OSS_PROCESS_SIGN_EXPIRES = int(
    os.environ.get("OSS_PROCESS_SIGN_EXPIRES", "21600")
)
OSS_MULTIPART_PART_SIZE_MB = max(
    1, int(os.environ.get("OSS_MULTIPART_PART_SIZE_MB", "8"))
)


def _oss_sub_prefix(env_name: str, fallback: str) -> str:
    value = os.environ.get(env_name, f"{OSS_PREFIX}{fallback}").strip().lstrip("/")
    if value and not value.endswith("/"):
        value += "/"
    return value


OSS_STAGING_PREFIX = _oss_sub_prefix("OSS_STAGING_PREFIX", "_staging/")
OSS_INGEST_PREFIX = _oss_sub_prefix("OSS_INGEST_PREFIX", "_ingest/")
OSS_DERIVED_PREFIX = _oss_sub_prefix("OSS_DERIVED_PREFIX", "_derived/")
OSS_STAGING_RETENTION_HOURS = float(
    os.environ.get("OSS_STAGING_RETENTION_HOURS", "24")
)
OSS_MANAGED_SOURCE_RETENTION_HOURS = float(
    os.environ.get("OSS_MANAGED_SOURCE_RETENTION_HOURS", "48")
)
# OSS 下载失败自动重试次数（不含首次）；≤0 关闭重试
OSS_DOWNLOAD_RETRIES = int(os.environ.get("OSS_DOWNLOAD_RETRIES", "3"))
# 下载重试基础等待秒数（指数退避：base * 2^attempt）
OSS_DOWNLOAD_RETRY_BASE_SLEEP = float(
    os.environ.get("OSS_DOWNLOAD_RETRY_BASE_SLEEP", "1.5")
)
# Optional comma-separated source host allowlist. Empty keeps public-URL mode.
IMPORT_ALLOWED_HOSTS = {
    item.strip().lower().rstrip(".")
    for item in os.environ.get("IMPORT_ALLOWED_HOSTS", "").split(",")
    if item.strip()
}

# 豆包 Seed 语义分镜（火山方舟）
ARK_API_KEY = os.environ.get("ARK_API_KEY", "").strip()
ARK_BASE_URL = os.environ.get(
    "ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"
).rstrip("/")
ARK_MODEL = os.environ.get("ARK_MODEL", "doubao-seed-2-1-pro-260628")
ARK_TIMEOUT = int(os.environ.get("ARK_TIMEOUT", "600"))
SEMANTIC_SAMPLE_FPS = float(os.environ.get("SEMANTIC_SAMPLE_FPS", "1.0"))

# 同进程后台任务并发上限（替代原 Celery worker concurrency）
INLINE_MAX_JOBS = int(os.environ.get("INLINE_MAX_JOBS", "4"))
# 单个切割任务内并行 ffmpeg 路数
CUT_MAX_WORKERS = int(os.environ.get("CUT_MAX_WORKERS", "4"))
# 内存账本上限（≤0 表示不限制）
MAX_INMEM_VIDEOS = int(os.environ.get("MAX_INMEM_VIDEOS", "200"))
MAX_INMEM_TASKS = int(os.environ.get("MAX_INMEM_TASKS", "500"))

# 卡住（pending/detecting/processing）超过该小时数则标记失败并清理 outputs
STALE_TASK_HOURS = float(os.environ.get("STALE_TASK_HOURS", "12"))
# done/preview 本地成品与源片缓存保留小时数；≤0 关闭按龄回收（磁盘水位仍可强制清）
LOCAL_RETENTION_HOURS = float(os.environ.get("LOCAL_RETENTION_HOURS", "48"))
# 周期清理间隔（分钟）；≤0 关闭定时（启动时仍会跑一轮）
CLEANUP_INTERVAL_MINUTES = float(os.environ.get("CLEANUP_INTERVAL_MINUTES", "30"))
# data 所在磁盘剩余低于该值（GiB）时，忽略保留期优先清最旧 done/preview；≤0 关闭
DISK_FREE_GB_MIN = float(os.environ.get("DISK_FREE_GB_MIN", "5"))
