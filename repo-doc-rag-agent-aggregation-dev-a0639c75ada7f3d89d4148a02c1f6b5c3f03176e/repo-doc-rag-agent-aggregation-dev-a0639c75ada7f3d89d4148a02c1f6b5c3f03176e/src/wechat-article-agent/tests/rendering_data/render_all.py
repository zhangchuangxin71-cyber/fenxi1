from __future__ import annotations

import hashlib
import html
import json
import shutil
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.rendering.wechat_layout import LayoutEngine  # noqa: E402
from app.rendering.wechat_layout.themes.registry import default_registry  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent
INPUT_DIR = DATA_DIR / "input"
OUTPUT_DIR = DATA_DIR / "output"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _verify_manifest() -> dict[str, Any]:
    manifest = _read_json(INPUT_DIR / "manifest.json")
    for name, metadata in manifest["files"].items():
        actual = _sha256(INPUT_DIR / name)
        if actual != metadata["sha256"]:
            raise RuntimeError(f"Fixture checksum mismatch: {name}")
    return manifest


def _index(results: list[dict[str, Any]]) -> str:
    navigation = "".join(
        (
            f'<a href="#{html.escape(item["theme_id"])}">'
            f"{html.escape(item['label'])} · {html.escape(item['theme_id'])}</a>"
        )
        for item in results
    )
    previews = "".join(
        f"""
        <section id="{html.escape(item["theme_id"])}">
          <header>
            <h2>{html.escape(item["label"])}</h2>
            <code>{html.escape(item["theme_id"])}</code>
            <span>{item["warning_count"]} warnings · {item["elapsed_ms"]:.3f} ms</span>
            <a href="{html.escape(item["theme_id"])}/final.html" target="_blank">单独打开</a>
          </header>
          <iframe
            title="{html.escape(item["label"])}"
            src="{html.escape(item["theme_id"])}/final.html"
          ></iframe>
        </section>
        """
        for item in results
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>微信公众号排版主题烟测</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; color: #17201d; font: 14px/1.5 sans-serif; background: #edf1ef; }}
    nav {{
      position: sticky; top: 0; z-index: 2; padding: 12px 20px;
      background: #fff; border-bottom: 1px solid #ccd6d1;
    }}
    nav a {{ display: inline-block; margin: 4px 14px 4px 0; color: #116955; }}
    main {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(390px, 1fr));
      gap: 20px; padding: 20px;
    }}
    section {{ min-width: 0; background: #fff; border: 1px solid #ccd6d1; }}
    header {{
      display: flex; flex-wrap: wrap; gap: 8px 14px; align-items: center;
      min-height: 56px; padding: 10px 14px; border-bottom: 1px solid #dce4e0;
    }}
    h2 {{ margin: 0; font-size: 16px; }}
    header span {{ color: #64736d; }}
    header a {{ margin-left: auto; color: #116955; }}
    iframe {{
      display: block; width: 390px; max-width: 100%; height: 760px;
      margin: 0 auto; border: 0; background: #fff;
    }}
    @media (max-width: 440px) {{ main {{ display: block; padding: 0; }} section {{ margin-bottom: 16px; }} }}
  </style>
</head>
<body>
  <nav>{navigation}</nav>
  <main>{previews}</main>
</body>
</html>
"""


def render_all() -> list[dict[str, Any]]:
    manifest = _verify_manifest()
    markdown = (INPUT_DIR / "article.md").read_text(encoding="utf-8")
    images = _read_json(INPUT_DIR / "images.json")
    task_spec = _read_json(INPUT_DIR / "task_spec.json")
    registry = default_registry()
    engine = LayoutEngine(registry=registry)

    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True)

    input_hashes = {name: value["sha256"] for name, value in manifest["files"].items()}
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    for theme in registry.all():
        result = engine.render(
            markdown=markdown,
            images=images,
            task_spec=task_spec,
            requested_theme_id=theme.id,
        )
        repeated = engine.render(
            markdown=markdown,
            images=images,
            task_spec=task_spec,
            requested_theme_id=theme.id,
        )
        deterministic = result.final_html == repeated.final_html
        theme_dir = OUTPUT_DIR / theme.id
        theme_dir.mkdir()
        (theme_dir / "final.html").write_text(result.final_html, encoding="utf-8")
        _write_json(theme_dir / "validation.json", result.validation_report.model_dump())
        metadata = {
            "theme_id": theme.id,
            "theme_version": theme.version,
            "theme_source": theme.source,
            "renderer_version": result.renderer_version,
            "input_sha256": input_hashes,
            "timings_ms": result.timings_ms,
            "fallback_used": result.fallback_used,
            "fallback_reason": result.fallback_reason,
            "deterministic": deterministic,
        }
        _write_json(theme_dir / "metadata.json", metadata)
        elapsed = sum(result.timings_ms.values())
        summary = {
            "theme_id": theme.id,
            "label": theme.label,
            "valid": result.validation_report.valid,
            "warning_count": len(result.validation_report.warnings),
            "fallback_used": result.fallback_used,
            "deterministic": deterministic,
            "elapsed_ms": elapsed,
        }
        results.append(summary)
        if not result.validation_report.valid or result.fallback_used or not deterministic:
            failures.append(theme.id)

    _write_json(
        OUTPUT_DIR / "summary.json",
        {
            "fixture_schema_version": manifest["schema_version"],
            "theme_count": len(results),
            "expected_theme_count": len(registry.ids()),
            "all_passed": not failures and len(results) == len(registry.ids()),
            "failed_themes": failures,
            "themes": results,
        },
    )
    (OUTPUT_DIR / "index.html").write_text(_index(results), encoding="utf-8")
    if failures or len(results) != len(registry.ids()):
        raise RuntimeError(f"Rendering smoke test failed: {failures}")
    return results


if __name__ == "__main__":
    rendered = render_all()
    print(json.dumps({"themes": len(rendered), "output": str(OUTPUT_DIR)}, ensure_ascii=False))
