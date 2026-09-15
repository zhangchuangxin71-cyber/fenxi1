INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>统一视频检索</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: system-ui, "PingFang SC", sans-serif; margin: 0; padding: 20px; background: #0f1218; color: #e8ecf0; }
    h1 { margin: 0 0 8px; font-size: 1.4rem; }
    .sub { color: #8a9bb0; margin-bottom: 12px; font-size: 14px; }
    #engineStatus { font-size: 13px; color: #9ab; margin-bottom: 14px; min-height: 1.2em; }
    .bar { display: flex; gap: 10px; margin-bottom: 16px; flex-wrap: wrap; align-items: center; }
    input[type=text] { flex: 1; min-width: 240px; padding: 12px 14px; border-radius: 8px; border: 1px solid #334; background: #1a2230; color: #fff; font-size: 16px; }
    select { padding: 10px 12px; border-radius: 8px; border: 1px solid #334; background: #1a2230; color: #fff; }
    button { padding: 12px 22px; border: 0; border-radius: 8px; background: #57c084; color: #08110c; font-weight: 600; cursor: pointer; }
    button:disabled { opacity: 0.5; }
    #status { min-height: 1.4em; color: #8ab; margin-bottom: 12px; }
    .columns { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; align-items: start; }
    .columns.single { grid-template-columns: 1fr; max-width: 720px; }
    @media (max-width: 1000px) { .columns { grid-template-columns: 1fr; } }
    .panel { background: #161d28; border: 1px solid #2a3544; border-radius: 12px; padding: 14px; }
    .panel.hidden { display: none; }
    .panel h2 { margin: 0 0 4px; font-size: 1.05rem; }
    .meta { font-size: 12px; color: #7a8fa8; margin-bottom: 12px; }
    .list { display: flex; flex-direction: column; gap: 12px; }
    .card { background: #1e2736; border-radius: 8px; overflow: hidden; border: 1px solid #2d3a4d; display: grid; grid-template-columns: 200px 1fr; gap: 0; }
    @media (max-width: 700px) { .card { grid-template-columns: 1fr; } }
    .card video { width: 100%; height: 112px; object-fit: cover; background: #000; display: block; }
    .card .thumb.missing { height: 112px; display: flex; align-items: center; justify-content: center; color: #f88; font-size: 11px; padding: 8px; text-align: center; }
    .card .info { padding: 10px; font-size: 12px; }
    .score { color: #6dd4a8; font-weight: 600; }
    .id { color: #9ab; word-break: break-all; margin: 4px 0; }
    .desc { color: #bcd; line-height: 1.4; max-height: 3.6em; overflow: hidden; }
    .tags { margin-top: 6px; display: flex; flex-wrap: wrap; gap: 4px; }
    .tag { background: #2a3544; padding: 2px 6px; border-radius: 4px; font-size: 11px; color: #9ab; }
    .rank-extra { color: #7a8fa8; font-size: 11px; margin-top: 4px; }
    .empty { color: #667; padding: 24px; text-align: center; }
    .warn { color: #e8a87c; }
  </style>
</head>
<body>
  <h1>统一视频检索</h1>
  <p class="sub">CLIP 视觉向量 · 豆包字幕稀疏+稠密混合 · 模式可选 clip / hybrid / both</p>
  <div id="engineStatus">引擎状态加载中…</div>
  <div class="bar">
    <input type="text" id="query" placeholder="输入检索词，例如：一个人在厨房做饭" spellcheck="false" />
    <select id="mode">
      <option value="both" selected>both · 双路对比</option>
      <option value="clip">clip · 仅 CLIP</option>
      <option value="hybrid">hybrid · 仅混合检索</option>
    </select>
    <button type="button" id="go">检索</button>
  </div>
  <div id="status"></div>
  <div class="columns" id="columns">
    <section class="panel" id="clipPanel">
      <h2>CLIP · Chinese-CLIP 帧/片段</h2>
      <div class="meta" id="clipMeta">—</div>
      <div class="list" id="clipList"></div>
    </section>
    <section class="panel" id="hybridPanel">
      <h2>Hybrid · 文字描述 + BGE</h2>
      <div class="meta" id="hybridMeta">—</div>
      <div class="list" id="hybridList"></div>
    </section>
  </div>
  <script>
    const statusEl = document.getElementById("status");
    const engineStatusEl = document.getElementById("engineStatus");
    const queryEl = document.getElementById("query");
    const modeEl = document.getElementById("mode");
    const goBtn = document.getElementById("go");
    const columnsEl = document.getElementById("columns");
    const clipPanel = document.getElementById("clipPanel");
    const hybridPanel = document.getElementById("hybridPanel");

    function escapeHtml(s) {
      return String(s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }

    function cardHtml(item, mode) {
      const id = escapeHtml(item.video_id || "");
      const score = Number(item.score || 0).toFixed(4);
      const desc = escapeHtml(item.display_line || item.description || "");
      const tags = (item.tags || []).map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join("");
      const extra = mode === "hybrid" && (item.sparse_rank || item.dense_rank)
        ? `<div class="rank-extra">稀疏 #${item.sparse_rank || "-"} · 稠密 #${item.dense_rank || "-"}</div>`
        : "";
      const segs = (item.segments || []).length
        ? `<div class="rank-extra">片段 ${(item.segments || []).map(s => s.start + "-" + s.end + "s").join(", ")}</div>`
        : "";
      const thumb = item.video_available !== true
        ? `<div class="thumb missing">视频缺失<br>${id}</div>`
        : `<video src="/media/${mode}/${encodeURIComponent(item.video_id || "")}" controls preload="metadata" playsinline></video>`;
      return `<article class="card">
        ${thumb}
        <div class="info">
          <div class="score">${score}</div>
          <div class="id">${id}</div>
          ${desc ? `<div class="desc">${desc}</div>` : ""}
          ${tags ? `<div class="tags">${tags}</div>` : ""}
          ${extra}${segs}
        </div>
      </article>`;
    }

    function renderList(listId, metaId, block, mode) {
      const list = document.getElementById(listId);
      const meta = document.getElementById(metaId);
      const items = (block && block.results) || [];
      let metaText = items.length
        ? `索引 ${block.index_count} · 用时 ${block.elapsed_ms} ms`
        : `无结果（索引 ${block ? block.index_count : 0}）`;
      if (mode === "hybrid" && block && !block.ready) {
        metaText += block.error ? ` · <span class="warn">${escapeHtml(block.error)}</span>` : " · 引擎加载中";
      }
      meta.innerHTML = metaText;
      list.innerHTML = items.length
        ? items.map((it) => cardHtml(it, mode)).join("")
        : '<div class="empty">无命中</div>';
    }

    function updateLayout(mode) {
      const both = mode === "both";
      clipPanel.classList.toggle("hidden", mode === "hybrid");
      hybridPanel.classList.toggle("hidden", mode === "clip");
      columnsEl.classList.toggle("single", !both);
    }

    async function refreshHealth() {
      try {
        const res = await fetch("/health");
        const h = await res.json();
        const clip = h.clip_ready ? "CLIP 就绪" : (h.clip_starting ? "CLIP 加载中…" : "CLIP 未加载");
        const hybrid = h.hybrid_ready ? "Hybrid 就绪" : (h.hybrid_starting ? "Hybrid 加载中…" : "Hybrid 未就绪");
        engineStatusEl.textContent =
          `CLIP 视频 ${h.clip_index_frames}（${h.clip_profile}）· ${clip} · Hybrid 视频 ${h.hybrid_index_videos}（${h.hybrid_profile}）· ${hybrid}` +
          (h.clip_error ? ` · CLIP: ${h.clip_error}` : "") +
          (h.hybrid_error ? ` · Hybrid: ${h.hybrid_error}` : "");
      } catch (e) {
        engineStatusEl.textContent = "状态获取失败";
      }
    }

    async function search() {
      const q = queryEl.value.trim();
      const mode = modeEl.value;
      if (!q) return;
      updateLayout(mode);
      goBtn.disabled = true;
      statusEl.textContent = "检索中…";
      try {
        const res = await fetch("/search", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query: q, mode: mode, top_k: 10 }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || res.status);
        statusEl.textContent = `「${data.query}」模式 ${data.mode} · 总用时 ${data.elapsed_ms} ms`;
        if (data.clip) renderList("clipList", "clipMeta", data.clip, "clip");
        if (data.hybrid) renderList("hybridList", "hybridMeta", data.hybrid, "hybrid");
      } catch (e) {
        statusEl.textContent = "错误: " + e.message;
      } finally {
        goBtn.disabled = false;
      }
    }

    modeEl.onchange = () => updateLayout(modeEl.value);
    goBtn.onclick = search;
    queryEl.addEventListener("keydown", (e) => { if (e.key === "Enter") search(); });
    updateLayout(modeEl.value);
    refreshHealth();
    setInterval(refreshHealth, 5000);
  </script>
</body>
</html>"""
