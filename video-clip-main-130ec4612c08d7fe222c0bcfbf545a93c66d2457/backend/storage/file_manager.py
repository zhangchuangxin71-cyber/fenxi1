import shutil
import uuid
from pathlib import Path

from backend.config import ALLOWED_EXTENSIONS, OUTPUT_DIR, UPLOAD_DIR, WORK_DIR


def ensure_dirs() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)


def generate_id() -> str:
    """标准 UUID（如 516436ac-ac25-46cf-b19e-c92e1ae0e13a）。"""
    return str(uuid.uuid4())


def is_allowed_extension(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def get_video_path(video_id: str) -> Path | None:
    for path in UPLOAD_DIR.glob(f"{video_id}.*"):
        if path.is_file():
            return path
    # OSS 工作区缓存
    work = WORK_DIR / video_id
    if work.is_dir():
        for path in work.glob("source.*"):
            if path.is_file():
                return path
    return None


def delete_video_file(video_id: str) -> None:
    from backend.core.filmstrip import delete_filmstrips
    from backend.core.waveform import delete_waveforms

    path = get_video_path(video_id)
    if path and path.exists():
        path.unlink()
    # 清理整个 work 目录（可能还有其它缓存）
    work = WORK_DIR / video_id
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    delete_filmstrips(video_id)
    delete_waveforms(video_id)


def get_task_output_dir(task_id: str) -> Path:
    path = OUTPUT_DIR / task_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def delete_task_outputs(task_id: str) -> None:
    path = OUTPUT_DIR / task_id
    if path.exists():
        shutil.rmtree(path)
