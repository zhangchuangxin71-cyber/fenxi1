from __future__ import annotations

import argparse
import tempfile
import time
from pathlib import Path

from pydantic import BaseModel, Field

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, HTMLResponse
except ImportError:  # pragma: no cover - optional until dual_search_service runs
    FastAPI = HTTPException = Request = FileResponse = HTMLResponse = None  # type: ignore

from .config import DEFAULT_MODEL_PATH, WORKSPACE_ROOT
from .retrieval import build_retriever
from .text_retrieval import build_text_retriever


def default_image_search_roots(primary: Path) -> list[Path]:
    """Primary UI image-dir plus optional local image roots for /media resolution."""
    roots: list[Path] = [primary.resolve()]
    for rel in (
        "data/muge/train_extracted",
        "data/muge/dataset_1k/images",
        "data/muge/dataset_1k_add/images",
    ):
        candidate = (WORKSPACE_ROOT / rel).resolve()
        if candidate.is_dir() and candidate not in roots:
            roots.append(candidate)
    return roots


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(default=10, ge=1, le=50)


class DualSearchService:
    def __init__(
        self,
        *,
        clip_profile: str,
        bge_profile: str,
        image_dir: Path,
        model_path: str,
        device: str = "cuda",
        bge_device: str = "cuda",
    ):
        self.search_roots = default_image_search_roots(image_dir)
        self.image_dir = self.search_roots[0]
        self.clip = build_retriever(
            profile=clip_profile,
            model_path=model_path,
            device=device,
            search_roots=self.search_roots,
        )
        self.bge = build_text_retriever(
            profile=bge_profile,
            bge_device=bge_device,
            search_roots=self.search_roots,
        )

    def _attach_media_flags(self, results: list[dict], *, mode: str) -> list[dict]:
        enriched = []
        for item in results:
            iid = str(item.get("image_id") or "")
            if mode == "clip":
                path = self.resolve_clip_path(iid)
            else:
                path = self.resolve_bge_path(iid)
            row = dict(item)
            row["image_available"] = path is not None
            if path is not None:
                row["resolved_path"] = str(path)
            enriched.append(row)
        return enriched

    def search(self, query: str, *, top_k: int = 10) -> dict:
        started = time.perf_counter()
        clip_results, clip_ms = self.clip.search_text(query, top_k=top_k)
        bge_results, bge_ms = self.bge.search_text(query, top_k=top_k)
        total_ms = (time.perf_counter() - started) * 1000.0
        clip_results = self._attach_media_flags(clip_results, mode="clip")
        bge_results = self._attach_media_flags(bge_results, mode="bge")
        return {
            "query": query,
            "top_k": top_k,
            "elapsed_ms": round(total_ms, 1),
            "image_roots": [str(p) for p in self.search_roots],
            "clip": {
                "mode": "chinese_clip",
                "index_count": self.clip.index_count(),
                "elapsed_ms": round(clip_ms, 1),
                "results": clip_results,
            },
            "bge": {
                "mode": "bge",
                "index_count": self.bge.index_count(),
                "elapsed_ms": round(bge_ms, 1),
                "results": bge_results,
            },
        }

    def search_by_image(
        self,
        image_path: Path,
        *,
        query: str | None = None,
        top_k: int = 10,
        query_label: str | None = None,
    ) -> dict:
        """CLIP 以图搜图（左栏）；若提供 query 则右栏仍做 BGE 文搜。"""
        started = time.perf_counter()
        clip_results, clip_ms = self.clip.search_image(image_path, top_k=top_k)
        clip_results = self._attach_media_flags(clip_results, mode="clip")

        text_query = (query or "").strip()
        if text_query:
            bge_results, bge_ms = self.bge.search_text(text_query, top_k=top_k)
            bge_results = self._attach_media_flags(bge_results, mode="bge")
            bge_mode = "bge"
        else:
            bge_results, bge_ms = [], 0.0
            bge_mode = "skipped"

        total_ms = (time.perf_counter() - started) * 1000.0
        display = query_label or f"以图搜图: {image_path.name}"
        return {
            "query": display,
            "clip_query_mode": "image",
            "top_k": top_k,
            "elapsed_ms": round(total_ms, 1),
            "image_roots": [str(p) for p in self.search_roots],
            "clip": {
                "mode": "chinese_clip_image",
                "index_count": self.clip.index_count(),
                "elapsed_ms": round(clip_ms, 1),
                "results": clip_results,
            },
            "bge": {
                "mode": bge_mode,
                "index_count": self.bge.index_count(),
                "elapsed_ms": round(bge_ms, 1),
                "results": bge_results,
                "hint": None if text_query else "以图搜图仅检索左侧 CLIP；填写文字可同时检索右侧 BGE。",
            },
        }

    def resolve_clip_path(self, image_id: str) -> Path | None:
        return self.clip.resolve_path(image_id)

    def resolve_bge_path(self, image_id: str) -> Path | None:
        return self.bge.resolve_path(image_id)

    def close(self) -> None:
        self.clip.close()
        self.bge.close()


def create_app(service: DualSearchService):
    if FastAPI is None:
        raise RuntimeError("fastapi is required: pip install fastapi uvicorn python-multipart")

    app = FastAPI(title="Picture Dual Search", version="1.0.0")

    @app.get("/", response_class=HTMLResponse)
    def home():
        return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>双方案文搜图对比</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: system-ui, sans-serif; margin: 0; padding: 20px; background: #0f1218; color: #e8ecf0; }
    h1 { margin: 0 0 8px; font-size: 1.4rem; }
    .sub { color: #8a9bb0; margin-bottom: 16px; font-size: 14px; }
    .bar { display: flex; gap: 10px; margin-bottom: 12px; flex-wrap: wrap; align-items: center; }
    input[type=text] { flex: 1; min-width: 240px; padding: 12px 14px; border-radius: 8px; border: 1px solid #334; background: #1a2230; color: #fff; font-size: 16px; }
    input[type=file] { font-size: 13px; color: #9ab; max-width: 220px; }
    .file-hint { font-size: 12px; color: #6a7f96; width: 100%; margin: 0 0 8px; }
    button { padding: 12px 22px; border: 0; border-radius: 8px; background: #3d9a6f; color: #fff; font-weight: 600; cursor: pointer; }
    button.btn-secondary { background: #2a3544; color: #c5d0de; padding: 10px 14px; font-size: 13px; font-weight: 500; }
    button.btn-secondary:hover { background: #354556; }
    button:disabled { opacity: 0.5; }
    .file-wrap { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .file-chosen { font-size: 12px; color: #8ab; max-width: 140px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    #status { min-height: 1.4em; color: #8ab; margin-bottom: 12px; }
    .columns { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; align-items: start; }
    @media (max-width: 900px) { .columns { grid-template-columns: 1fr; } }
    .panel { background: #161d28; border: 1px solid #2a3544; border-radius: 12px; padding: 14px; }
    .panel h2 { margin: 0 0 4px; font-size: 1.05rem; }
    .meta { font-size: 12px; color: #7a8fa8; margin-bottom: 12px; }
    .grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }
    @media (min-width: 1200px) { .grid { grid-template-columns: repeat(3, 1fr); } }
    .card { background: #1e2736; border-radius: 8px; overflow: hidden; border: 1px solid #2d3a4d; }
    .card .thumb { width: 100%; height: 120px; background: #0a0e14; display: flex; align-items: center; justify-content: center; overflow: hidden; }
    .card img { width: 100%; height: 120px; object-fit: cover; background: #000; display: block; }
    .card .thumb.missing { color: #f66; font-size: 11px; padding: 8px; text-align: center; }
    .card .info { padding: 8px; font-size: 12px; }
    .score { color: #6dd4a8; font-weight: 600; }
    .id { color: #9ab; word-break: break-all; }
    .tags { color: #bcd; margin-top: 4px; line-height: 1.35; max-height: 2.7em; overflow: hidden; }
    .empty { color: #667; padding: 24px; text-align: center; }
    .view-hidden { display: none !important; }
    .img-search-layout { display: grid; grid-template-columns: 200px 1fr; gap: 20px; align-items: start; }
    @media (max-width: 720px) { .img-search-layout { grid-template-columns: 1fr; } }
    .query-preview { background: #161d28; border: 1px solid #2a3544; border-radius: 12px; padding: 12px; text-align: center; }
    .query-preview h2 { margin: 0 0 8px; font-size: 0.95rem; color: #9ab; font-weight: 600; }
    .query-preview img { max-width: 100%; max-height: 200px; border-radius: 8px; object-fit: contain; background: #0a0e14; }
    .query-preview .fname { font-size: 11px; color: #6a7f96; margin-top: 8px; word-break: break-all; }
    .img-results-panel { background: #161d28; border: 1px solid #2a3544; border-radius: 12px; padding: 14px; }
    .img-results-panel h2 { margin: 0 0 4px; font-size: 1.05rem; }
    .grid-img { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 12px; }
    .card-img { background: #1e2736; border-radius: 8px; overflow: hidden; border: 1px solid #2d3a4d; position: relative; }
    .card-img .rank { position: absolute; top: 6px; left: 6px; background: rgba(15,18,24,0.85); color: #6dd4a8; font-size: 11px; font-weight: 700; padding: 2px 6px; border-radius: 4px; z-index: 1; }
    .card-img .thumb { height: 140px; }
    .card-img img { height: 140px; }
    .img-bge-addon { margin-top: 16px; padding-top: 14px; border-top: 1px dashed #2a3544; }
    .img-bge-addon h3 { margin: 0 0 6px; font-size: 0.9rem; color: #8a9bb0; font-weight: 600; }
  </style>
</head>
<body>
  <h1>图片检索对比</h1>
  <p class="sub" id="subText">文搜图：左 Chinese-CLIP · 右 BGE，各 Top 10</p>
  <p class="sub view-hidden" id="subImage">以图搜图：Chinese-CLIP 视觉相似（与上方双方案文搜图独立）</p>
  <p class="file-hint" id="fileHint">可选上传 query 图：有图时进入以图搜图视图；仅文字时仍为双方案对比。</p>
  <div class="bar">
    <input type="text" id="query" placeholder="文搜图：输入检索词，例如：橘猫 沙发" data-gramm="false" spellcheck="false" />
    <div class="file-wrap">
      <input type="file" id="imageFile" accept="image/*" title="以图搜图 query 图" />
      <span id="fileChosen" class="file-chosen view-hidden"></span>
      <button type="button" id="clearFile" class="btn-secondary view-hidden">清除图片</button>
    </div>
    <button type="button" id="go">检索</button>
  </div>
  <div id="status"></div>

  <div id="textSearchView">
    <div class="columns">
      <section class="panel">
        <h2>方案 A · Chinese-CLIP</h2>
        <div class="meta" id="clipMeta">—</div>
        <div class="grid" id="clipGrid"></div>
      </section>
      <section class="panel">
        <h2>方案 B · BGE</h2>
        <div class="meta" id="bgeMeta">—</div>
        <div class="grid" id="bgeGrid"></div>
      </section>
    </div>
  </div>

  <div id="imageSearchView" class="view-hidden">
    <div class="img-search-layout">
      <aside class="query-preview">
        <h2>Query 图</h2>
        <img id="queryPreview" alt="query" />
        <div class="fname" id="queryFname">—</div>
        <button type="button" id="clearFilePreview" class="btn-secondary view-hidden" style="margin-top:10px;width:100%;">清除 query 图</button>
      </aside>
      <section class="img-results-panel">
        <h2>视觉相似 · Chinese-CLIP</h2>
        <div class="meta" id="clipMetaImg">—</div>
        <div class="grid-img" id="clipGridImg"></div>
        <div id="imageBgeAddon" class="img-bge-addon view-hidden">
          <h3>附带文搜图 · BGE</h3>
          <div class="meta" id="bgeMetaImg">—</div>
          <div class="grid" id="bgeGridImg"></div>
        </div>
      </section>
    </div>
  </div>

  <script>
    const statusEl = document.getElementById("status");
    const queryEl = document.getElementById("query");
    const fileEl = document.getElementById("imageFile");
    const clearFileBtn = document.getElementById("clearFile");
    const clearFilePreviewBtn = document.getElementById("clearFilePreview");
    const fileChosenEl = document.getElementById("fileChosen");
    const goBtn = document.getElementById("go");
    const textSearchView = document.getElementById("textSearchView");
    const imageSearchView = document.getElementById("imageSearchView");
    const subText = document.getElementById("subText");
    const subImage = document.getElementById("subImage");
    let queryPreviewUrl = null;

    function escapeHtml(s) {
      return String(s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }

    function cardHtml(item, mode) {
      const id = escapeHtml(item.image_id || "");
      const imgSrc = `/media/${mode}/${encodeURIComponent(item.image_id || "")}`;
      const tags = escapeHtml(item.display_line || item.description || "");
      const thumb = item.image_available === false
        ? `<div class="thumb missing">图片缺失<br>${id}</div>`
        : `<div class="thumb"><img src="${imgSrc}" alt="" loading="lazy"
             onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'thumb missing',innerHTML:'加载失败<br>${id}'}))" /></div>`;
      return `<div class="card">
        ${thumb}
        <div class="info">
          <div class="score">${Number(item.score).toFixed(4)}</div>
          <div class="id">${id}</div>
          ${tags ? `<div class="tags">${tags}</div>` : ""}
        </div>
      </div>`;
    }

    function renderColumn(gridId, metaId, block, mode) {
      const grid = document.getElementById(gridId);
      const meta = document.getElementById(metaId);
      const items = (block && block.results) || [];
      const hint = block && block.hint ? ` · ${block.hint}` : "";
      meta.textContent = items.length
        ? `索引 ${block.index_count} 条 · 用时 ${block.elapsed_ms} ms${hint}`
        : (block && block.hint) || `无结果（索引 ${block ? block.index_count : 0} 条）`;
      if (!items.length) {
        grid.innerHTML = '<div class="empty">无命中</div>';
        return;
      }
      grid.innerHTML = items.map((it) => cardHtml(it, mode)).join("");
    }

    function setSearchView(mode) {
      const isImage = mode === "image";
      textSearchView.classList.toggle("view-hidden", isImage);
      imageSearchView.classList.toggle("view-hidden", !isImage);
      subText.classList.toggle("view-hidden", isImage);
      subImage.classList.toggle("view-hidden", !isImage);
    }

    function updateFileUi() {
      const file = fileEl.files && fileEl.files[0];
      const has = !!file;
      clearFileBtn.classList.toggle("view-hidden", !has);
      clearFilePreviewBtn.classList.toggle("view-hidden", !has);
      fileChosenEl.classList.toggle("view-hidden", !has);
      if (has) fileChosenEl.textContent = file.name;
    }

    function clearSelectedFile() {
      fileEl.value = "";
      if (queryPreviewUrl) {
        URL.revokeObjectURL(queryPreviewUrl);
        queryPreviewUrl = null;
      }
      const preview = document.getElementById("queryPreview");
      preview.removeAttribute("src");
      document.getElementById("queryFname").textContent = "—";
      updateFileUi();
      setSearchView("text");
      statusEl.textContent = "已清除 query 图，可进行双方案文搜图";
    }

    function cardHtmlImage(item, rank) {
      const id = escapeHtml(item.image_id || "");
      const imgSrc = `/media/clip/${encodeURIComponent(item.image_id || "")}`;
      const thumb = item.image_available === false
        ? `<div class="thumb missing">图片缺失<br>${id}</div>`
        : `<div class="thumb"><img src="${imgSrc}" alt="" loading="lazy"
             onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'thumb missing',innerHTML:'加载失败<br>${id}'}))" /></div>`;
      return `<div class="card-img">
        <span class="rank">#${rank}</span>
        ${thumb}
        <div class="info">
          <div class="score">相似度 ${Number(item.score).toFixed(4)}</div>
          <div class="id">${id}</div>
        </div>
      </div>`;
    }

    function renderImageSearch(data, queryFile) {
      setSearchView("image");
      const preview = document.getElementById("queryPreview");
      const fname = document.getElementById("queryFname");
      if (queryPreviewUrl) {
        URL.revokeObjectURL(queryPreviewUrl);
        queryPreviewUrl = null;
      }
      if (queryFile) {
        queryPreviewUrl = URL.createObjectURL(queryFile);
        preview.src = queryPreviewUrl;
        fname.textContent = queryFile.name || "upload";
      } else {
        preview.removeAttribute("src");
        fname.textContent = "—";
      }
      const clip = data.clip || {};
      const items = clip.results || [];
      const clipMeta = document.getElementById("clipMetaImg");
      const clipGrid = document.getElementById("clipGridImg");
      clipMeta.textContent = items.length
        ? `索引 ${clip.index_count} 条 · 用时 ${clip.elapsed_ms} ms · 按视觉向量相似度排序`
        : `无结果（索引 ${clip.index_count || 0} 条）`;
      clipGrid.innerHTML = items.length
        ? items.map((it, i) => cardHtmlImage(it, i + 1)).join("")
        : '<div class="empty">无命中</div>';

      const bgeAddon = document.getElementById("imageBgeAddon");
      const bge = data.bge || {};
      const bgeItems = bge.results || [];
      const hasTextBge = bgeItems.length > 0;
      const showBgeAddon = hasTextBge || (bge.hint && queryEl.value.trim());
      bgeAddon.classList.toggle("view-hidden", !showBgeAddon);
      if (showBgeAddon) {
        if (hasTextBge) {
          renderColumn("bgeGridImg", "bgeMetaImg", bge, "bge");
        } else {
          document.getElementById("bgeMetaImg").textContent = bge.hint || "—";
          document.getElementById("bgeGridImg").innerHTML = '<div class="empty">未填写检索词</div>';
        }
      }
    }

    function renderTextSearch(data) {
      setSearchView("text");
      renderColumn("clipGrid", "clipMeta", data.clip, "clip");
      renderColumn("bgeGrid", "bgeMeta", data.bge, "bge");
    }

    async function search() {
      const q = queryEl.value.trim();
      const file = fileEl.files && fileEl.files[0];
      if (!file && !q) return;
      goBtn.disabled = true;
      statusEl.textContent = "检索中…";
      try {
        let res;
        let usedFile = null;
        if (file) {
          usedFile = file;
          const form = new FormData();
          form.append("file", file);
          if (q) form.append("query", q);
          form.append("top_k", "10");
          res = await fetch("/search-by-image", { method: "POST", body: form });
        } else {
          res = await fetch("/search", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ query: q, top_k: 10 }),
          });
        }
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || res.status);
        if (data.clip_query_mode === "image") {
          statusEl.textContent = `以图搜图 · ${data.query} · 总用时 ${data.elapsed_ms} ms`;
          renderImageSearch(data, usedFile);
        } else {
          statusEl.textContent = `「${data.query}」总用时 ${data.elapsed_ms} ms`;
          renderTextSearch(data);
        }
      } catch (e) {
        statusEl.textContent = "错误: " + e.message;
      } finally {
        goBtn.disabled = false;
      }
    }
    goBtn.onclick = search;
    clearFileBtn.onclick = clearSelectedFile;
    clearFilePreviewBtn.onclick = clearSelectedFile;
    fileEl.addEventListener("change", updateFileUi);
    queryEl.addEventListener("keydown", (e) => { if (e.key === "Enter") search(); });
    updateFileUi();
  </script>
</body>
</html>"""

    @app.post("/search")
    def search(req: SearchRequest):
        return service.search(req.query, top_k=req.top_k)

    @app.post("/search-by-image")
    async def search_by_image(http_request: Request):
        form = await http_request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(400, detail="请上传图片文件 (field: file)")
        content_type = str(getattr(upload, "content_type", "") or "")
        if content_type and not content_type.startswith("image/"):
            raise HTTPException(400, detail="请上传图片文件")
        filename = str(getattr(upload, "filename", "") or "query.jpg")
        suffix = Path(filename).suffix or ".jpg"
        data = await upload.read()
        if not data:
            raise HTTPException(400, detail="空文件")
        raw_query = form.get("query")
        query_text = raw_query.strip() if isinstance(raw_query, str) and raw_query.strip() else None
        try:
            top_k = max(1, min(int(form.get("top_k", 10)), 50))
        except (TypeError, ValueError):
            top_k = 10
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(data)
                tmp_path = Path(tmp.name)
            return service.search_by_image(
                tmp_path,
                query=query_text,
                top_k=top_k,
                query_label=f"以图搜图: {filename}",
            )
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    @app.get("/media/clip/{image_id:path}")
    def media_clip(image_id: str):
        from urllib.parse import unquote

        image_id = unquote(image_id).strip()
        path = service.resolve_clip_path(image_id)
        if path is None:
            raise HTTPException(404, detail=f"image not found: {image_id}")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/media/bge/{image_id:path}")
    def media_bge(image_id: str):
        from urllib.parse import unquote

        image_id = unquote(image_id).strip()
        path = service.resolve_bge_path(image_id)
        if path is None:
            raise HTTPException(404, detail=f"image not found: {image_id}")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/health")
    def health():
        return {
            "clip_index": service.clip.index_count(),
            "bge_index": service.bge.index_count(),
        }

    return app


def parse_args():
    p = argparse.ArgumentParser(description="Dual picture text search (CLIP + BGE side by side).")
    p.add_argument("--clip-profile", default="muge_train_clip")
    p.add_argument("--bge-profile", default="muge_1k_bge")
    p.add_argument(
        "--image-dir",
        required=True,
        help="Primary image directory; also auto-tries train_extracted, dataset_1k_add.",
    )
    p.add_argument("--model-path", default=str(DEFAULT_MODEL_PATH))
    p.add_argument("--device", default="cuda")
    p.add_argument("--bge-device", default="cuda")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8022)
    return p.parse_args()


def main():
    import uvicorn

    args = parse_args()
    service = DualSearchService(
        clip_profile=args.clip_profile,
        bge_profile=args.bge_profile,
        image_dir=Path(args.image_dir),
        model_path=args.model_path,
        device=args.device,
        bge_device=args.bge_device,
    )
    app = create_app(service)
    print(f"[dual_search] http://{args.host}:{args.port}/", flush=True)
    print(f"[dual_search] clip={args.clip_profile} bge={args.bge_profile}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
