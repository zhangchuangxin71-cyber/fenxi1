from __future__ import annotations

import argparse
import importlib
import shutil
from pathlib import Path

REQUIRED_PACKAGES = [
    ("numpy", "numpy"),
    ("torch", "torch"),
    ("transformers", "transformers"),
    ("PIL", "pillow"),
    ("cv2", "opencv-python"),
    ("pymilvus", "pymilvus"),
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
]

OPTIONAL_PACKAGES: list[tuple[str, str]] = []


def parse_args():
    parser = argparse.ArgumentParser(
        description="Validate runtime environment for video_processing."
    )
    parser.add_argument(
        "--model-path", default="", help="Optional local Chinese-CLIP model directory."
    )
    parser.add_argument(
        "--ffmpeg-binary", default="ffmpeg", help="ffmpeg executable name or absolute path."
    )
    return parser.parse_args()


def check_package(import_name: str) -> tuple[bool, str]:
    try:
        module = importlib.import_module(import_name)
    except Exception as exc:
        return False, str(exc)
    version = getattr(module, "__version__", "unknown")
    return True, str(version)


def check_ffmpeg(binary: str) -> tuple[bool, str]:
    resolved = shutil.which(binary) if binary == "ffmpeg" else str(Path(binary))
    if not resolved:
        return False, "not found in PATH"
    return True, resolved


def check_model_path(model_path: str) -> tuple[bool, str]:
    if not model_path:
        return False, "not provided"
    path = Path(model_path)
    if not path.exists():
        return False, "path does not exist"
    required = ["config.json"]
    missing = [name for name in required if not (path / name).exists()]
    if missing:
        return False, f"missing files: {missing}"
    return True, str(path.resolve())


def print_status_block(title: str, lines: list[str]):
    print(title)
    for line in lines:
        print(f"  {line}")
    print("")


def print_check_group(title: str, items: list[tuple[str, str]], optional: bool = False):
    lines = []
    for import_name, package_name in items:
        ok, detail = check_package(import_name)
        lines.append(f"[{'OK' if ok else 'WARN' if optional else 'FAIL'}] {package_name}: {detail}")
    print_status_block(title, lines)


def main():
    args = parse_args()

    print("Video Processing Environment Check")
    print("")

    ffmpeg_ok, ffmpeg_detail = check_ffmpeg(args.ffmpeg_binary)
    print_status_block("System", [f"[{'OK' if ffmpeg_ok else 'FAIL'}] ffmpeg: {ffmpeg_detail}"])

    print_check_group("Required Packages", REQUIRED_PACKAGES, optional=False)
    if OPTIONAL_PACKAGES:
        print_check_group("Optional Packages", OPTIONAL_PACKAGES, optional=True)

    model_ok, model_detail = check_model_path(args.model_path)
    print_status_block("Model", [f"[{'OK' if model_ok else 'WARN'}] model_path: {model_detail}"])

    if not ffmpeg_ok:
        print_status_block("Action", ["Install ffmpeg and make sure it is available in PATH."])
        return

    print("Done")


if __name__ == "__main__":
    main()
