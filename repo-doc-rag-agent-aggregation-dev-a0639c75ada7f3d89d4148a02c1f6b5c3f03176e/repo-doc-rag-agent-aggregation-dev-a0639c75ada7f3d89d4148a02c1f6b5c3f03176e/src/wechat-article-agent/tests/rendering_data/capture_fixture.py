from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings  # noqa: E402

INPUT_DIR = Path(__file__).resolve().parent / "input"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def capture(artifact_id: str | None) -> None:
    settings = get_settings()
    query_filter = "AND artifact_id = %s" if artifact_id else ""
    parameters = (artifact_id,) if artifact_id else ()
    with psycopg.connect(str(settings.database_url)) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT article_markdown, images, task_spec
                  FROM article_artifacts
                 WHERE status = 'completed'
                   AND article_markdown IS NOT NULL
                   AND task_spec IS NOT NULL
                   AND jsonb_array_length(COALESCE(images, '[]'::jsonb)) > 0
                   {query_filter}
                 ORDER BY updated_at DESC
                 LIMIT 1
                """,  # noqa: S608 - the only interpolation is a fixed SQL fragment
                parameters,
            )
            row = cursor.fetchone()
    if row is None:
        raise RuntimeError("No completed artifact with Markdown, task spec, and images was found.")

    article_markdown, images, task_spec = row
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    (INPUT_DIR / "article.md").write_text(article_markdown, encoding="utf-8")
    _write_json(INPUT_DIR / "images.json", images)
    _write_json(INPUT_DIR / "task_spec.json", task_spec)

    files = ["article.md", "images.json", "task_spec.json"]
    manifest = {
        "schema_version": 1,
        "captured_at": datetime.now(UTC).isoformat(),
        "source": "development article_artifacts database",
        "contains_business_identifiers": False,
        "files": {name: {"sha256": _sha256(INPUT_DIR / name)} for name in files},
    }
    _write_json(INPUT_DIR / "manifest.json", manifest)
    print(
        json.dumps(
            {
                "input_dir": str(INPUT_DIR),
                "article_chars": len(article_markdown),
                "image_count": len(images),
            },
            ensure_ascii=False,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture a real renderer fixture from the development DB.")
    parser.add_argument(
        "--artifact-id",
        help="Optional development artifact ID. Defaults to the newest eligible completed artifact.",
    )
    arguments = parser.parse_args()
    capture(arguments.artifact_id)


if __name__ == "__main__":
    main()
