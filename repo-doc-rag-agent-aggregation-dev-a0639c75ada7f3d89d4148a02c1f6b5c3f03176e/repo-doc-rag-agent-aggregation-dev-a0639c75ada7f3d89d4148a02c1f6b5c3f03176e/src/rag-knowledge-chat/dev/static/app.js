const requestEditor = document.querySelector("#request-json");
const conversation = document.querySelector("#conversation");
const timeline = document.querySelector("#timeline");
const rawEvents = document.querySelector("#raw-events");
const retrievalDebug = document.querySelector("#retrieval-debug");
const stateLabel = document.querySelector("#connection-state");
const sendButton = document.querySelector("#send-button");
const traceTabs = [...document.querySelectorAll(".trace-tab")];
const traceViews = [...document.querySelectorAll(".trace-view")];
const workflowStages = new Map();

function pretty(value) {
  return JSON.stringify(value, null, 2);
}

function setThinking(active) {
  stateLabel.classList.toggle("thinking", active);
}

function formatElapsed(milliseconds) {
  return `${(milliseconds / 1000).toFixed(2)}s`;
}

function finishStageTimer(stage) {
  const active = workflowStages.get(stage);
  if (!active) return;
  active.node.classList.remove("active");
  active.node.classList.add("complete");
  active.duration.textContent = formatElapsed(performance.now() - active.startedAt);
  workflowStages.delete(stage);
}

function startStageTimer(stage, message, assistant) {
  if (workflowStages.has(stage)) return;
  if (stage === "thinking") finishStageTimer("retrieval");
  const node = document.createElement("div");
  node.className = "workflow-status active";
  const label = document.createElement("span");
  label.textContent = message;
  const duration = document.createElement("span");
  duration.className = "stage-duration";
  duration.textContent = "...";
  node.append(label, duration);
  const bubble = assistant.closest(".message");
  bubble.insertBefore(node, assistant);
  workflowStages.set(stage, {node, duration, startedAt: performance.now()});
}

function addMessage(role, text = "") {
  const row = document.createElement("div");
  row.className = `message-row ${role}`;

  const avatar = document.createElement("div");
  avatar.className = "message-avatar";
  avatar.setAttribute("aria-hidden", "true");
  avatar.textContent = role === "user" ? "U" : "AI";

  const bubble = document.createElement("div");
  bubble.className = "message";
  const content = document.createElement("div");
  content.className = "message-content";
  content.textContent = text;
  bubble.appendChild(content);
  row.append(avatar, bubble);
  conversation.appendChild(row);
  conversation.scrollTop = conversation.scrollHeight;
  return content;
}

function appendReasoning(assistant, delta) {
  const bubble = assistant.closest(".message");
  if (!bubble || !delta) return;
  let details = bubble.querySelector(":scope > .reasoning-details");
  if (!details) {
    details = document.createElement("details");
    details.className = "reasoning-details";
    const summary = document.createElement("summary");
    summary.textContent = "思考过程";
    const content = document.createElement("pre");
    content.className = "reasoning-content";
    details.append(summary, content);
    bubble.insertBefore(details, assistant);
  }
  details.querySelector(".reasoning-content").textContent += delta;
}

function addTrace(stage, detail = "") {
  const node = document.createElement("div");
  node.className = "trace-item";
  const title = document.createElement("strong");
  title.textContent = stage;
  const text = document.createElement("span");
  text.textContent = detail;
  node.append(title, text);
  timeline.appendChild(node);
}

function setTraceView(name) {
  traceTabs.forEach(tab => {
    const active = tab.dataset.traceView === name;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  traceViews.forEach(view => {
    const active = view.dataset.traceView === name;
    view.classList.toggle("active", active);
    view.hidden = !active;
  });
}

function appendMetric(container, label, value) {
  const item = document.createElement("div");
  item.className = "trace-metric";
  const name = document.createElement("span");
  name.textContent = label;
  const content = document.createElement("strong");
  content.textContent = value === undefined || value === null || value === "" ? "-" : String(value);
  item.append(name, content);
  container.appendChild(item);
}

function appendTraceSection(container, title) {
  const heading = document.createElement("h3");
  heading.textContent = title;
  container.appendChild(heading);
}

function appendJsonDetails(container, title, value, open = false) {
  const details = document.createElement("details");
  details.className = "debug-details";
  details.open = open;
  const summary = document.createElement("summary");
  summary.textContent = title;
  const content = document.createElement("pre");
  content.textContent = pretty(value);
  details.append(summary, content);
  container.appendChild(details);
  return details;
}

function appendDebugCard(container, title, subtitle = "", status = "") {
  const card = document.createElement("div");
  card.className = `debug-card ${status ? `status-${status}` : ""}`;
  const header = document.createElement("div");
  header.className = "debug-card-header";
  const heading = document.createElement("strong");
  heading.textContent = title;
  const meta = document.createElement("span");
  meta.textContent = subtitle;
  header.append(heading, meta);
  card.appendChild(header);
  container.appendChild(card);
  return card;
}

function renderRetrievalTrace(retrievalEnvelope = {}, retrievalMeta = {}) {
  retrievalDebug.replaceChildren();
  const debug = retrievalEnvelope.debug;
  const usage = retrievalEnvelope.usage || {};
  if (!debug) {
    const empty = document.createElement("p");
    empty.className = "trace-empty";
    empty.textContent = retrievalEnvelope.error
      ? `检索失败：${retrievalEnvelope.error.code || "unknown"}`
      : "本次响应未包含检索 debug；请确认服务端与请求均已启用 debug。";
    retrievalDebug.appendChild(empty);
    return;
  }

  const overview = document.createElement("div");
  overview.className = "trace-metrics";
  appendMetric(overview, "状态", retrievalMeta.status);
  appendMetric(overview, "请求 ID", debug.request_id || retrievalMeta.request_id);
  appendMetric(overview, "模式", debug.actual_mode || usage.actual_mode);
  appendMetric(overview, "LLM 调用", usage.llm_request_count);
  appendMetric(overview, "返回 chunks", usage.returned_count);
  appendMetric(overview, "返回 tokens", usage.returned_tokens);
  appendMetric(overview, "总耗时", usage.latency_ms === undefined ? null : `${usage.latency_ms} ms`);
  retrievalDebug.appendChild(overview);

  const input = debug.input || {};
  appendTraceSection(retrievalDebug, "检索服务收到的问题（知识库问答改写后）");
  const query = document.createElement("div");
  query.className = "debug-query";
  query.textContent = Array.isArray(input.query)
    ? input.query.map((item, index) => `${index + 1}. ${item}`).join("\n")
    : input.query || "-";
  retrievalDebug.appendChild(query);
  const inputMeta = document.createElement("p");
  inputMeta.className = "debug-subtle";
  inputMeta.textContent = [
    `请求模式：${input.search_mode || "-"}`,
    `top_k：${input.top_k ?? "-"}`,
    `max_return_tokens：${input.max_return_tokens ?? "-"}`,
    `scope：${input.scope_document_count ?? "-"} 篇`,
  ].join(" · ");
  retrievalDebug.appendChild(inputMeta);

  const classificationTrace = Array.isArray(debug.classification_trace)
    ? debug.classification_trace
    : [];
  if (classificationTrace.length) {
    appendTraceSection(retrievalDebug, "Robust 分类内部过程");
    const phaseList = document.createElement("div");
    phaseList.className = "debug-card-list";
    classificationTrace.forEach((phase, index) => {
      const card = appendDebugCard(
        phaseList,
        `${index + 1}. ${phase.phase || "unknown"}`,
        "ROBUST",
        "ok",
      );
      appendJsonDetails(card, "完整阶段数据", phase);
    });
    retrievalDebug.appendChild(phaseList);
  }

  const groups = Array.isArray(debug.groups) ? debug.groups : [];
  if (groups.length) {
    appendTraceSection(retrievalDebug, "分类结果");
    const groupList = document.createElement("div");
    groupList.className = "debug-card-list";
    groups.forEach(group => {
      const card = appendDebugCard(
        groupList,
        `${group.group_ref || "group"} · ${group.category || "unknown"}`,
        group.degraded ? "DEGRADED" : "",
        group.degraded ? "degraded" : "ok",
      );
      const questions = document.createElement("p");
      questions.textContent = (group.queries || []).join("；") || "-";
      card.appendChild(questions);
      if (group.target_docs_description || group.target_docs_keywords?.length) {
        appendJsonDetails(card, "文档路由参数", {
          target_docs_description: group.target_docs_description,
          target_docs_keywords: group.target_docs_keywords,
        });
      }
    });
    retrievalDebug.appendChild(groupList);
  }

  const routes = Array.isArray(debug.routes) ? debug.routes : [];
  if (routes.length) {
    appendTraceSection(retrievalDebug, "文档路由");
    const routeList = document.createElement("div");
    routeList.className = "debug-card-list";
    routes.forEach(route => {
      const subtitle = [
        `accept ${route.accept_docs?.length || 0}`,
        `possible ${route.possible_docs?.length || 0}`,
        `reject ${route.rejected_document_count || 0}`,
        `prefilter ${route.prefilter_candidate_count || 0}`,
      ].join(" · ");
      const card = appendDebugCard(
        routeList,
        route.group_ref || "route",
        subtitle,
        route.degraded ? "degraded" : "ok",
      );
      appendJsonDetails(card, "完整路由决策", route);
    });
    retrievalDebug.appendChild(routeList);
  }

  const summary = debug.state_summary || {};
  if (Object.keys(summary).length) {
    appendTraceSection(retrievalDebug, "状态摘要");
    const summaryGrid = document.createElement("div");
    summaryGrid.className = "trace-summary-grid";
    Object.entries(summary).forEach(([key, value]) => {
      appendMetric(
        summaryGrid,
        key,
        typeof value === "object" && value !== null ? JSON.stringify(value) : value,
      );
    });
    retrievalDebug.appendChild(summaryGrid);
  }

  const durations = Object.entries(debug.node_durations_ms || {}).sort((a, b) => b[1] - a[1]);
  if (durations.length) {
    appendTraceSection(retrievalDebug, "节点耗时");
    const durationList = document.createElement("div");
    durationList.className = "duration-list";
    const maximum = Math.max(...durations.map(([, value]) => Number(value) || 0), 1);
    durations.forEach(([node, value]) => {
      const row = document.createElement("div");
      row.className = "duration-row";
      const label = document.createElement("span");
      label.textContent = node;
      const track = document.createElement("div");
      track.className = "duration-track";
      const bar = document.createElement("div");
      bar.className = "duration-bar";
      bar.style.width = `${Math.max(2, (Number(value) / maximum) * 100)}%`;
      track.appendChild(bar);
      const duration = document.createElement("strong");
      duration.textContent = `${value} ms`;
      row.append(label, track, duration);
      durationList.appendChild(row);
    });
    retrievalDebug.appendChild(durationList);
  }

  const trace = Array.isArray(debug.trace) ? debug.trace : [];
  if (trace.length) {
    appendTraceSection(retrievalDebug, "执行事件");
    const events = document.createElement("div");
    events.className = "retrieval-events";
    trace.forEach(item => {
      const event = document.createElement("div");
      event.className = `retrieval-event status-${item.status || "unknown"}`;
      const title = document.createElement("div");
      title.className = "retrieval-event-title";
      const name = document.createElement("strong");
      name.textContent = item.node || item.phase || "unknown";
      const status = document.createElement("span");
      status.textContent = item.status || "unknown";
      title.append(name, status);
      const detail = document.createElement("p");
      const parts = [];
      if (item.duration_ms !== null && item.duration_ms !== undefined) parts.push(`${item.duration_ms} ms`);
      if (item.fallback_used) parts.push("使用 fallback");
      if (item.group_refs?.length) parts.push(`groups: ${item.group_refs.join(", ")}`);
      if (item.document_ids?.length) parts.push(`documents: ${item.document_ids.length}`);
      if (item.error_category) parts.push(`error: ${item.error_category}`);
      detail.textContent = parts.join(" · ") || item.phase || "";
      event.append(title, detail);
      events.appendChild(event);
    });
    retrievalDebug.appendChild(events);
  }

  const llmCalls = Array.isArray(debug.llm_calls) ? debug.llm_calls : [];
  if (llmCalls.length) {
    appendTraceSection(retrievalDebug, "LLM 调用明细");
    const callList = document.createElement("div");
    callList.className = "debug-card-list";
    llmCalls.forEach(call => {
      const elapsed = (call.queue_wait_ms || 0) + (call.provider_duration_ms || 0);
      const card = appendDebugCard(
        callList,
        `#${call.sequence || "?"} ${call.phase || "unknown"}`,
        `${call.status || "unknown"} · ${elapsed} ms · ${call.prompt_tokens || 0}/${call.completion_tokens || 0} tokens`,
        call.status || "unknown",
      );
      appendJsonDetails(card, "LLM 请求", call.request || {});
      const toolCalls = call.response?.tool_calls || call.response?.raw_output?.tool_calls || [];
      if (toolCalls.length) appendJsonDetails(card, "工具调用", toolCalls, true);
      const toolResults = (call.request?.messages || []).filter(message => message.role === "tool");
      if (toolResults.length) appendJsonDetails(card, "工具结果", toolResults, true);
      appendJsonDetails(card, call.status === "failed" ? "失败详情" : "LLM 真实输出", call.response || {});
    });
    retrievalDebug.appendChild(callList);
  }

  const toolEvents = Array.isArray(debug.tool_events) ? debug.tool_events : [];
  if (toolEvents.length) {
    appendTraceSection(retrievalDebug, "工具执行过程");
    const toolList = document.createElement("div");
    toolList.className = "debug-card-list";
    toolEvents.forEach(event => {
      const status = event.kind === "fallback" ? "degraded" : "ok";
      const card = appendDebugCard(
        toolList,
        `#${event.sequence || "?"} ${event.node || "tool"} · ${event.kind || "event"}`,
        [event.group_ref, event.document_id].filter(Boolean).join(" · "),
        status,
      );
      appendJsonDetails(card, event.kind === "tool_call" ? "工具调用" : "工具结果", event.payload || {}, true);
    });
    retrievalDebug.appendChild(toolList);
  }

  const candidates = Array.isArray(debug.candidates) ? debug.candidates : [];
  if (candidates.length) {
    appendTraceSection(retrievalDebug, `候选 chunks（${candidates.length}）`);
    const candidateList = document.createElement("div");
    candidateList.className = "debug-card-list";
    candidates.forEach((candidate, index) => {
      const card = appendDebugCard(
        candidateList,
        `${index + 1}. ${candidate.document_name || candidate.source_type || "chunk"}`,
        `${candidate.category || "-"} · ${candidate.page_number ? `第 ${candidate.page_number} 页` : candidate.path || "-"}`,
      );
      appendJsonDetails(card, "候选内容与元数据", candidate);
    });
    retrievalDebug.appendChild(candidateList);
  }

  const raw = document.createElement("details");
  const rawTitle = document.createElement("summary");
  rawTitle.textContent = "完整原始检索 debug";
  const rawContent = document.createElement("pre");
  rawContent.textContent = pretty({usage, debug});
  raw.append(rawTitle, rawContent);
  retrievalDebug.appendChild(raw);
}

function formatLocation(reference) {
  if (reference.page_number !== undefined && reference.page_number !== null) {
    return `第 ${reference.page_number} 页`;
  }
  const meta = reference.chunk_meta || {};
  if (meta.page_number !== undefined && meta.page_number !== null) {
    return `第 ${meta.page_number} 页`;
  }
  const title = meta.title || meta.node_title;
  if (title) return `标题：${title}`;
  if (meta.node_id) {
    let range = "";
    if (meta.start_index !== undefined && meta.start_index !== null) {
      const end = meta.end_index;
      range = end !== undefined && end !== null && end !== meta.start_index
        ? ` · 第 ${meta.start_index}-${end} 页`
        : ` · 第 ${meta.start_index} 页`;
    }
    return `节点 ${meta.node_id}${range}`;
  }
  return reference.path || "位置未知";
}

function renderReferences(message, references = [], chunks = [], debugCandidates = []) {
  message.querySelector(".reference-list")?.remove();
  if (!references.length) return;

  const byIndex = new Map(chunks.map(item => [item.citation_index, item]));
  const byId = new Map(chunks.map(item => [item.chunk_id, item]));
  const debugById = new Map(debugCandidates.map(item => [item.chunk_id, item]));
  const list = document.createElement("div");
  list.className = "reference-list";
  const heading = document.createElement("strong");
  heading.textContent = "引用来源";
  list.appendChild(heading);

  references.forEach(reference => {
    const chunk = byIndex.get(reference.citation_index) || byId.get(reference.chunk_id);
    const debugChunk = debugById.get(reference.chunk_id);
    const details = document.createElement("details");
    details.className = "reference-item";
    const summary = document.createElement("summary");
    const documentName = reference.doc_name || chunk?.document_name || "未命名文档";
    summary.textContent = `[${reference.citation_index}] ${documentName} · ${formatLocation(reference)}`;
    const preview = document.createElement("p");
    const content = chunk?.content || "暂无原文预览";
    const contentPreview = content.length > 600 ? `${content.slice(0, 600)}...` : content;
    const hint = chunk?.hint || debugChunk?.hint;
    const previewParts = [];
    if (hint) previewParts.push(`检索提示：${hint}`);
    previewParts.push(contentPreview);
    preview.textContent = previewParts.join("\n\n");
    details.append(summary, preview);
    list.appendChild(details);
  });
  message.appendChild(list);
}

function handleEvent(payload, assistant) {
  const choice = payload.choices?.[0];
  const reasoning = choice?.delta?.reasoning_content || choice?.delta?.reasoningContent;
  if (reasoning) appendReasoning(assistant, reasoning);
  const delta = choice?.delta?.content;
  if (delta) {
    finishStageTimer("thinking");
    assistant.textContent += delta;
    if (stateLabel.classList.contains("thinking")) stateLabel.textContent = "正在回答";
    setThinking(false);
  }
  const rag = payload.rag;
  if (rag?.event) {
    stateLabel.textContent = rag.event.message;
    const stage = rag.event.stage;
    setThinking(stage === "thinking");
    if (stage === "retrieval" || stage === "thinking") {
      startStageTimer(stage, rag.event.message, assistant);
    } else {
      finishStageTimer("retrieval");
      finishStageTimer("thinking");
    }
    addTrace(rag.event.stage, rag.event.message);
  }
  if (rag?.debug?.trace) {
    rag.debug.trace.forEach(item => addTrace(item.stage, `${item.elapsed_ms} ms`));
  }
  if (rag?.debug?.retrieval) {
    renderRetrievalTrace(rag.debug.retrieval, rag.retrieval || {});
  }
  if (rag?.answer_basis) {
    finishStageTimer("retrieval");
    finishStageTimer("thinking");
    setThinking(false);
    const debugCandidates = rag.debug?.retrieval?.debug?.candidates || [];
    renderReferences(assistant, rag.references, rag.chunks, debugCandidates);
    addTrace("completed", rag.answer_basis);
  }
  if (rag?.error) {
    finishStageTimer("retrieval");
    finishStageTimer("thinking");
    setThinking(false);
    stateLabel.textContent = "Error";
    addTrace("error", `${rag.error.code}: ${rag.error.message}`);
  }
}

async function loadConfig() {
  const response = await fetch("/api/config");
  const config = await response.json();
  document.querySelector("#manifest-dir").value = config.manifest_dir;
}

document.querySelector("#generate-button").addEventListener("click", async () => {
  const response = await fetch("/api/generate-request", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      user_id: document.querySelector("#user-id").value,
      kb_id: document.querySelector("#kb-id").value,
      noise_count: Number(document.querySelector("#noise-count").value),
      top_k: Number(document.querySelector("#top-k").value),
      max_return_tokens: Number(document.querySelector("#max-return-tokens").value),
      seed: Date.now() % 100000,
      manifest_dir: document.querySelector("#manifest-dir").value
    })
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "生成请求失败");
  requestEditor.value = pretty(payload);
});

document.querySelector("#format-button").addEventListener("click", () => {
  requestEditor.value = pretty(JSON.parse(requestEditor.value));
});

document.querySelector("#clear-button").addEventListener("click", () => {
  conversation.replaceChildren();
  workflowStages.clear();
  timeline.replaceChildren();
  rawEvents.textContent = "";
  retrievalDebug.replaceChildren();
  const empty = document.createElement("p");
  empty.className = "trace-empty";
  empty.textContent = "尚无检索 trace";
  retrievalDebug.appendChild(empty);
  stateLabel.textContent = "Ready";
  setThinking(false);
});

traceTabs.forEach(tab => {
  tab.addEventListener("click", () => setTraceView(tab.dataset.traceView));
});

sendButton.addEventListener("click", async () => {
  const payload = JSON.parse(requestEditor.value);
  const userText = [...payload.messages].reverse().find(item => item.role === "user")?.content || "";
  addMessage("user", userText);
  const assistant = addMessage("assistant");
  workflowStages.clear();
  timeline.replaceChildren();
  rawEvents.textContent = "";
  stateLabel.textContent = "Connecting";
  setThinking(false);
  sendButton.disabled = true;
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    if (!response.ok) throw new Error(await response.text());
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const {done, value} = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), {stream: !done});
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop() || "";
      for (const block of blocks) {
        if (!block.startsWith("data: ")) continue;
        const raw = block.slice(6);
        rawEvents.textContent += `${raw}\n`;
        if (raw === "[DONE]") {
          setThinking(false);
          stateLabel.textContent = "Done";
          continue;
        }
        handleEvent(JSON.parse(raw), assistant);
      }
      if (done) break;
    }
  } catch (error) {
    setThinking(false);
    stateLabel.textContent = "Error";
    assistant.textContent += `\n请求失败：${error.message}`;
  } finally {
    sendButton.disabled = false;
  }
});

loadConfig().catch(error => {
  stateLabel.textContent = `Config error: ${error.message}`;
});
