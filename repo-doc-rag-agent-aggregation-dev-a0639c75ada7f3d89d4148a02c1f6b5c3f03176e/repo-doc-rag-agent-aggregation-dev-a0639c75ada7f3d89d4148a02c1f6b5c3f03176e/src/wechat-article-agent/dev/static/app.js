const state = {
  selectedDocuments: [],
  history: [],
  turns: [],
  currentTurn: null,
  responseId: null,
  runId: null,
  artifactId: null,
  revision: null,
  controller: null,
  receiving: false,
  rawEvents: [],
  traceByRun: new Map(),
  disclosureState: new Map(),
  timelineViewDirty: true,
  llmViewDirty: true,
  toolViewDirty: true,
  debugViewDirty: true,
  eventsViewDirty: true,
  forceTraceViewRender: true,
};

const $ = (selector) => document.querySelector(selector);
const RENDER_INTERVAL_MS = 50;
const TRACE_VIEW_MAX_CHARS = 2_000_000;
let renderTimer = null;
let conversationDirty = false;
let traceDirty = false;
let eventsRenderTimer = null;
let lastEventsRenderAt = 0;

function requestRender({conversation = true, trace = true, immediate = false} = {}) {
  conversationDirty ||= conversation;
  traceDirty ||= trace;
  if (immediate) {
    flushRender();
    return;
  }
  if (renderTimer !== null) return;
  renderTimer = window.setTimeout(flushRender, RENDER_INTERVAL_MS);
}

function flushRender() {
  if (renderTimer !== null) window.clearTimeout(renderTimer);
  renderTimer = null;
  if (conversationDirty) renderConversation();
  if (traceDirty) renderTrace();
  conversationDirty = false;
  traceDirty = false;
}

function setConnection(label, status = "") {
  const target = $("#connection-state");
  target.textContent = label;
  target.className = `status-dot ${status}`.trim();
}

function formatJson(value) {
  return JSON.stringify(value, null, 2);
}

function formattedValue(value) {
  if (typeof value !== "string") return formatJson(value);
  const trimmed = value.trim();
  if ((trimmed.startsWith("{") && trimmed.endsWith("}"))
      || (trimmed.startsWith("[") && trimmed.endsWith("]"))) {
    try {
      return formatJson(JSON.parse(trimmed));
    } catch (_) {
      return value;
    }
  }
  return value;
}

function defaultSessionId() {
  const timestamp = new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14);
  return `wechat-dev-${timestamp}`;
}

function textFromMessage(message) {
  if (typeof message.content === "string") return message.content;
  if (!Array.isArray(message.content)) return "";
  return message.content.map((part) => part.text || "").join("\n");
}

function normalizeHistory(input) {
  if (!Array.isArray(input)) return [];
  return input.map((message) => ({role: message.role, content: textFromMessage(message)}));
}

function latestUserText(payload) {
  const messages = normalizeHistory(payload.input);
  return messages.findLast((item) => item.role === "user")?.content || "";
}

function createTurn(payload) {
  const turn = {
    localId: `turn-${state.turns.length + 1}`,
    responseId: null,
    runId: null,
    artifactId: null,
    revision: null,
    status: "connecting",
    userText: latestUserText(payload),
    assistantText: "",
    reasoningText: "",
    activities: new Map(),
    artifacts: new Map(),
    outputs: [],
    interrupt: null,
    error: null,
    debug: null,
    rawEvents: [],
    renderVersion: 1,
  };
  state.turns.push(turn);
  state.currentTurn = turn;
  return turn;
}

function touchTurn(turn) {
  turn.renderVersion = (turn.renderVersion || 0) + 1;
}

function ensureOutput(turn, type, key, value) {
  let output = turn.outputs.find((item) => item.type === type && item.key === key);
  if (!output) {
    output = {type, key, value};
    turn.outputs.push(output);
  } else {
    output.value = value;
  }
  return output;
}

function restoreDisclosure(details, key, defaultOpen = false) {
  details.open = state.disclosureState.has(key)
    ? state.disclosureState.get(key)
    : defaultOpen;
  details.addEventListener("toggle", () => {
    // A detached <details> can dispatch its queued toggle event after a token-driven
    // rerender. Never let that stale element overwrite the user's current choice.
    if (details.isConnected) state.disclosureState.set(key, details.open);
  });
}

function activeInterruptTurn() {
  return [...state.turns].reverse().find((turn) => turn.interrupt?.status === "pending") || null;
}

function renderDocuments() {
  const root = $("#document-preview");
  root.replaceChildren();
  if (!state.selectedDocuments.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "尚未选择文档";
    root.append(empty);
    return;
  }
  for (const documentInfo of state.selectedDocuments) {
    const card = document.createElement("article");
    card.className = "document-card";
    const title = document.createElement("strong");
    title.textContent = documentInfo.doc_name || documentInfo.doc_id;
    const metadata = document.createElement("p");
    metadata.textContent = [
      documentInfo.doc_type,
      documentInfo.page_count ? `${documentInfo.page_count} 页` : null,
      documentInfo.node_count ? `${documentInfo.node_count} 节点` : null,
      documentInfo.status,
    ].filter(Boolean).join(" · ");
    const description = document.createElement("p");
    description.textContent = documentInfo.doc_description || "无文档摘要";
    const id = document.createElement("code");
    id.textContent = documentInfo.doc_id;
    card.append(title, metadata, description, id);
    root.append(card);
  }
}

function buildRequest() {
  const userInput = state.history.findLast((item) => item.role === "user")?.content
    || "请根据这些参考文档生成一篇微信公众号文章。";
  return {
    model: "wechat-article-agent",
    stream: true,
    input: state.history.length ? state.history : [{role: "user", content: userInput}],
    context: {
      session_id: $("#session-id").value.trim(),
      user_id: $("#user-id").value.trim(),
      kb_id: $("#kb-id").value.trim(),
      doc_ids: state.selectedDocuments.map((item) => item.doc_id),
      temp_doc_ids: [],
      debug: true,
    },
  };
}

function messageRow(role, text) {
  const row = document.createElement("div");
  row.className = `message-row ${role}`;
  const bubble = document.createElement("div");
  bubble.className = "message-bubble";
  bubble.textContent = text;
  row.append(bubble);
  return row;
}

function isCallableActivity(activity) {
  return activity.kind === "tool" || activity.kind === "skill";
}

function callableType(activity) {
  return activity.kind === "skill" || activity.tool_type === "skill" ? "skill" : "tool";
}

function formatElapsed(milliseconds) {
  if (!Number.isFinite(milliseconds)) return "";
  if (milliseconds < 1) return "< 1 ms";
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
  if (milliseconds < 60_000) return `${(milliseconds / 1000).toFixed(milliseconds < 10_000 ? 2 : 1)} s`;
  const minutes = Math.floor(milliseconds / 60_000);
  const seconds = Math.round((milliseconds % 60_000) / 1000);
  return `${minutes} min ${seconds} s`;
}

function renderActivityList(turn) {
  const list = document.createElement("div");
  list.className = "activity-list";
  for (const activity of [...turn.activities.values()].filter((item) => !isCallableActivity(item))) {
    const item = document.createElement("div");
    item.className = `activity ${activity.status || ""}`;
    const label = document.createElement("span");
    label.textContent = activity.label || activity.name || "工作流活动";
    const status = document.createElement("span");
    status.textContent = activity.status || "";
    item.append(label, status);
    list.append(item);
  }
  return list;
}

function publicActivityField(label, value) {
  const field = document.createElement("div");
  field.className = "callable-activity-field";
  const title = document.createElement("span");
  title.textContent = label;
  const content = document.createElement("pre");
  content.textContent = formattedValue(value);
  field.append(title, content);
  return field;
}

function renderCallableActivities(turn) {
  const section = document.createElement("div");
  section.className = "callable-activity-list";
  const heading = document.createElement("div");
  heading.className = "callable-activity-heading";
  heading.textContent = "工具与 Skill";
  section.append(heading);

  for (const activity of [...turn.activities.values()].filter(isCallableActivity)) {
    const type = callableType(activity);
    const card = document.createElement("details");
    card.className = `callable-activity ${type} ${activity.status || ""}`;
    restoreDisclosure(card, `callable:${turn.localId}:${activity.id}`);
    const summary = document.createElement("summary");
    const badge = document.createElement("span");
    badge.className = `callable-badge ${type}`;
    badge.textContent = type === "skill" ? "Skill" : "工具";
    const title = document.createElement("strong");
    title.textContent = activity.label || activity.name || "调用外部能力";
    const meta = document.createElement("span");
    meta.className = "callable-meta";
    meta.textContent = [activity.status, formatElapsed(activity._elapsedMs)].filter(Boolean).join(" · ");
    summary.append(badge, title, meta);
    card.append(summary);

    const body = document.createElement("div");
    body.className = "callable-activity-body";
    const context = document.createElement("p");
    context.textContent = `${activity.node || "workflow"} · ${activity.name || "call"}`;
    body.append(context);
    if (activity.input_summary != null) body.append(publicActivityField("调用摘要", activity.input_summary));
    if (activity.output_summary != null) body.append(publicActivityField("结果摘要", activity.output_summary));
    if (activity.error != null) body.append(publicActivityField("错误", activity.error));
    card.append(body);
    section.append(card);
  }
  return section;
}

function renderReasoning(text, key) {
  const details = document.createElement("details");
  details.className = "reasoning";
  restoreDisclosure(details, `reasoning:${key}`, true);
  const summary = document.createElement("summary");
  summary.textContent = "模型思考";
  const pre = document.createElement("pre");
  pre.textContent = text;
  details.append(summary, pre);
  return details;
}

function downloadHtml(content, artifact) {
  const blob = new Blob([content], {type: "text/html;charset=utf-8"});
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `wechat-article-r${artifact.revision || 1}.html`;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function renderArtifact(artifact, key) {
  const card = document.createElement("details");
  card.className = `artifact-card artifact-${artifact.stage || "unknown"}`;
  restoreDisclosure(card, `artifact:${key}`, artifact.stage === "material_sources");
  const header = document.createElement("summary");
  header.className = "artifact-header";
  const stage = document.createElement("span");
  stage.textContent = artifactStageLabel(artifact.stage);
  const status = document.createElement("span");
  status.textContent = `revision ${artifact.revision ?? "-"} · ${artifact.status || ""}`;
  header.append(stage, status);
  card.append(header);

  if (artifact.stage === "material_sources" && Array.isArray(artifact.content)) {
    card.append(renderMaterialSources(artifact.content));
  } else if (artifact.stage === "material_conflicts") {
    card.append(renderMaterialConflicts(artifact.content));
  } else if (artifact.stage === "final_html" && typeof artifact.content === "string") {
    const toolbar = document.createElement("div");
    toolbar.className = "artifact-toolbar";
    const download = document.createElement("button");
    download.type = "button";
    download.textContent = "下载 HTML";
    download.addEventListener("click", () => downloadHtml(artifact.content, artifact));
    toolbar.append(download);
    const frame = document.createElement("iframe");
    frame.className = "html-frame";
    frame.setAttribute("sandbox", "allow-popups");
    frame.srcdoc = artifact.content;
    frame.title = "最终微信公众号 HTML";
    card.append(toolbar, frame);
  } else {
    const content = document.createElement("div");
    content.className = "artifact-content";
    content.textContent = typeof artifact.content === "string"
      ? artifact.content
      : formatJson(artifact.content ?? artifact.summary ?? null);
    card.append(content);
  }
  return card;
}

function artifactStageLabel(stage) {
  return {
    session_memory: "历史审批偏好",
    material_library: "原始素材模型（Debug）",
    material_sources: "本轮参考素材来源",
    material_conflicts: "待处理的素材冲突",
    task_spec: "文章任务书",
    outline: "文章大纲",
    article_markdown: "未排版文章",
    images: "文章配图",
    final_html: "最终 HTML",
  }[stage] || stage || "artifact";
}

function renderMaterialSources(sources) {
  const body = document.createElement("div");
  body.className = "material-source-list";
  if (!sources.length) {
    const empty = document.createElement("p");
    empty.className = "material-source-empty";
    empty.textContent = "本轮未使用外部参考素材";
    body.append(empty);
    return body;
  }
  for (const source of sources) {
    const row = document.createElement("div");
    row.className = "material-source-row";
    const badge = document.createElement("span");
    const isUrl = typeof source === "string" && /^https?:\/\//i.test(source);
    badge.className = `material-source-badge ${isUrl ? "web" : "document"}`;
    badge.textContent = isUrl ? "网页" : "文档";
    if (isUrl) {
      const link = document.createElement("a");
      link.href = source;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = source;
      row.append(badge, link);
    } else {
      const name = document.createElement("span");
      name.textContent = String(source);
      row.append(badge, name);
    }
    body.append(row);
  }
  return body;
}

function conflictGroups(content) {
  if (Array.isArray(content)) return content;
  return Array.isArray(content?.conflicts) ? content.conflicts : [];
}

function renderMaterialConflicts(content) {
  const body = document.createElement("div");
  body.className = "material-conflict-list";
  for (const [index, conflict] of conflictGroups(content).entries()) {
    const group = document.createElement("section");
    group.className = "material-conflict-group";
    const title = document.createElement("strong");
    title.textContent = `冲突 ${index + 1}`;
    const message = document.createElement("p");
    message.textContent = conflict.message || conflict.user_facing_message || "以下素材存在事实冲突。";
    group.append(title, message);
    for (const source of conflict.sources || []) {
      const sourceCard = document.createElement("div");
      sourceCard.className = "material-conflict-source";
      const path = document.createElement("strong");
      path.textContent = source.path || source.ref || source.chunk_id || "素材来源";
      sourceCard.append(path);
      if (source.excerpt) {
        const excerpt = document.createElement("p");
        excerpt.textContent = source.excerpt;
        sourceCard.append(excerpt);
      }
      group.append(sourceCard);
    }
    body.append(group);
  }
  if (!body.children.length) {
    const empty = document.createElement("p");
    empty.className = "material-source-empty";
    empty.textContent = "冲突详情暂不可用，请根据审批卡片继续。";
    body.append(empty);
  }
  return body;
}

function renderInterrupt(interrupt, turn) {
  const card = document.createElement("article");
  card.className = `interrupt-card ${interrupt.status !== "pending" ? "resolved" : ""}`;
  const header = document.createElement("div");
  header.className = "artifact-header";
  const title = document.createElement("span");
  title.textContent = interrupt.form?.title || "需要您的决策";
  const status = document.createElement("span");
  status.textContent = interrupt.status === "pending"
    ? "等待处理"
    : interrupt.decision === "cancelled"
      ? "已取消 · 结果已过期"
      : interrupt.form?.form_type === "agent_clarification"
        ? "已提交 · 已确认"
        : `已提交 · ${decisionLabel(interrupt.decision)}`;
  header.append(title, status);
  const body = document.createElement("div");
  body.className = "interrupt-body";
  const description = document.createElement("div");
  description.textContent = interrupt.form?.description || "请审核当前结果。";
  body.append(description);
  const isClarificationSelection = interrupt.form?.form_type === "agent_clarification"
    && Array.isArray(interrupt.form?.options);
  const isIntentSelection = isClarificationSelection
    && interrupt.form?.clarification_type === "intent_confirmation";
  const isConflictReview = interrupt.form?.form_type === "agent_artifact_review"
    && interrupt.form?.review_type === "material_conflict";
  if (isConflictReview && Array.isArray(interrupt.form?.fields)) {
    body.append(renderMaterialConflicts({conflicts: interrupt.form.fields}));
  } else if (!isClarificationSelection && Array.isArray(interrupt.form?.fields) && interrupt.form.fields.length) {
    const fields = document.createElement("pre");
    fields.textContent = formatJson(interrupt.form.fields);
    body.append(fields);
  }
  if (interrupt.status === "pending") {
    if (isClarificationSelection) {
      body.append(isIntentSelection
        ? renderIntentTopicForm(interrupt, turn)
        : renderResearchDirectionForm(interrupt, turn));
      card.append(header, body);
      return card;
    }
    if (isConflictReview) {
      body.append(renderConflictResolutionForm(interrupt, turn));
      card.append(header, body);
      return card;
    }
    const feedback = document.createElement("textarea");
    feedback.className = "interrupt-feedback";
    feedback.placeholder = interrupt.form?.form_type === "agent_clarification"
      ? "请一次性填写所有需要确认的信息"
      : "修改反馈（选择按反馈修改时必填）";
    const actions = document.createElement("div");
    actions.className = "interrupt-actions";
    if (interrupt.form?.form_type === "agent_clarification") {
      actions.append(decisionButton("提交答复", "revise", feedback, turn));
    } else {
      actions.append(
        decisionButton("接受", "approve", feedback, turn, "primary"),
        decisionButton("按反馈修改", "revise", feedback, turn),
        decisionButton("完全重生成", "regenerate", feedback, turn),
      );
    }
    body.append(feedback, actions);
  }
  card.append(header, body);
  return card;
}

function renderIntentTopicForm(interrupt, turn) {
  const form = document.createElement("div");
  form.className = "research-direction-form intent-topic-form";
  const groupName = `intent-topic-${turn.localId}-${interrupt.id}`;
  for (const option of interrupt.form.options) {
    const label = document.createElement("label");
    label.className = "direction-option";
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = groupName;
    radio.value = option.id;
    const content = document.createElement("span");
    const heading = document.createElement("strong");
    heading.textContent = `方案 ${option.id}`;
    const topic = document.createElement("span");
    topic.textContent = `围绕“${option.topic}”生成微信公众号文章`;
    content.append(heading, topic);
    label.append(radio, content);
    form.append(label);
  }

  const customLabel = document.createElement("label");
  customLabel.className = "direction-option custom";
  const customRadio = document.createElement("input");
  customRadio.type = "radio";
  customRadio.name = groupName;
  customRadio.value = "custom";
  const customTitle = document.createElement("strong");
  customTitle.textContent = "其他：自定义文章主题";
  customLabel.append(customRadio, customTitle);
  form.append(customLabel);

  const customFields = document.createElement("div");
  customFields.className = "direction-custom-fields hidden";
  const topicInput = document.createElement("textarea");
  topicInput.placeholder = interrupt.form.custom_option?.topic_label || "其他文章主题";
  customFields.append(topicInput);
  form.append(customFields);
  for (const radio of form.querySelectorAll(`input[name="${groupName}"]`)) {
    radio.addEventListener("change", () => {
      customFields.classList.toggle("hidden", radio.value !== "custom" || !radio.checked);
    });
  }

  const actions = document.createElement("div");
  actions.className = "interrupt-actions";
  const submit = document.createElement("button");
  submit.type = "button";
  submit.className = "primary";
  submit.textContent = "确认文章方向";
  submit.addEventListener("click", () => {
    const selected = form.querySelector(`input[name="${groupName}"]:checked`);
    if (!selected) {
      setConnection("请选择一个文章主题", "failed");
      return;
    }
    const selection = selected.value === "custom"
      ? {option_id: "custom", topic: topicInput.value.trim()}
      : {option_id: selected.value};
    if (selection.option_id === "custom" && !selection.topic) {
      setConnection("请填写文章主题", "failed");
      return;
    }
    submitDecision(turn, "revise", "", selection);
  });
  actions.append(submit);
  form.append(actions);
  return form;
}

function renderResearchDirectionForm(interrupt, turn) {
  const form = document.createElement("div");
  form.className = "research-direction-form";
  const groupName = `research-direction-${turn.localId}-${interrupt.id}`;
  for (const option of interrupt.form.options) {
    const label = document.createElement("label");
    label.className = "direction-option";
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = groupName;
    radio.value = option.id;
    const content = document.createElement("span");
    const heading = document.createElement("strong");
    heading.textContent = `方案 ${option.id}`;
    const description = document.createElement("span");
    description.textContent = `本次将搜集与“${option.target}”主题有关的素材，重点关注“${option.about}”方面，其余无关内容将忽略。`;
    content.append(heading, description);
    label.append(radio, content);
    form.append(label);
  }
  const customLabel = document.createElement("label");
  customLabel.className = "direction-option custom";
  const customRadio = document.createElement("input");
  customRadio.type = "radio";
  customRadio.name = groupName;
  customRadio.value = "custom";
  const customTitle = document.createElement("strong");
  customTitle.textContent = "其他：自定义素材搜集方向";
  customLabel.append(customRadio, customTitle);
  form.append(customLabel);

  const customFields = document.createElement("div");
  customFields.className = "direction-custom-fields hidden";
  const aboutInput = document.createElement("textarea");
  aboutInput.placeholder = interrupt.form.custom_option?.about_label || "素材关注方面";
  const targetInput = document.createElement("textarea");
  targetInput.placeholder = interrupt.form.custom_option?.target_label || "素材搜集主题";
  customFields.append(aboutInput, targetInput);
  form.append(customFields);
  for (const radio of form.querySelectorAll(`input[name="${groupName}"]`)) {
    radio.addEventListener("change", () => {
      customFields.classList.toggle("hidden", radio.value !== "custom" || !radio.checked);
    });
  }

  const actions = document.createElement("div");
  actions.className = "interrupt-actions";
  const submit = document.createElement("button");
  submit.type = "button";
  submit.className = "primary";
  submit.textContent = "确认素材方向";
  submit.addEventListener("click", () => {
    const selected = form.querySelector(`input[name="${groupName}"]:checked`);
    if (!selected) {
      setConnection("请选择一个素材搜集方向", "failed");
      return;
    }
    const selection = selected.value === "custom"
      ? {option_id: "custom", about: aboutInput.value.trim(), target: targetInput.value.trim()}
      : {option_id: selected.value};
    if (selection.option_id === "custom" && (!selection.about || !selection.target)) {
      setConnection("请填写素材搜集主题和关注方面", "failed");
      return;
    }
    submitDecision(turn, "revise", "", selection);
  });
  actions.append(submit);
  form.append(actions);
  return form;
}

function renderConflictResolutionForm(interrupt, turn) {
  const form = document.createElement("div");
  form.className = "research-direction-form conflict-resolution-form";
  const groupName = `material-conflict-${turn.localId}-${interrupt.id}`;
  for (const option of interrupt.form.options || []) {
    const label = document.createElement("label");
    label.className = "direction-option";
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = groupName;
    radio.value = option.id;
    const text = document.createElement("span");
    const title = document.createElement("strong");
    title.textContent = option.label || option.id;
    text.append(title);
    label.append(radio, text);
    form.append(label);
  }
  const feedback = document.createElement("textarea");
  feedback.className = "interrupt-feedback hidden";
  feedback.placeholder = interrupt.form.custom_option?.feedback_label || "补充冲突处理意见";
  form.append(feedback);
  for (const radio of form.querySelectorAll(`input[name="${groupName}"]`)) {
    radio.addEventListener("change", () => {
      feedback.classList.toggle("hidden", radio.value !== "custom_feedback" || !radio.checked);
    });
  }
  const actions = document.createElement("div");
  actions.className = "interrupt-actions";
  const submit = document.createElement("button");
  submit.type = "button";
  submit.className = "primary";
  submit.textContent = "确认处理方式";
  submit.addEventListener("click", () => {
    const selected = form.querySelector(`input[name="${groupName}"]:checked`);
    if (!selected) {
      setConnection("请选择一种冲突处理方式", "failed");
      return;
    }
    const text = feedback.value.trim();
    if (selected.value === "custom_feedback" && !text) {
      setConnection("请填写冲突处理意见", "failed");
      return;
    }
    submitDecision(turn, "revise", text, {option_id: selected.value});
  });
  actions.append(submit);
  form.append(actions);
  return form;
}

function decisionButton(label, decision, feedback, turn, className = "") {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.className = className;
  button.addEventListener("click", () => submitDecision(turn, decision, feedback.value.trim()));
  return button;
}

function renderError(error) {
  const banner = document.createElement("article");
  banner.className = "error-banner";
  const title = document.createElement("strong");
  title.textContent = `${error.code || "运行错误"}${error.stage ? ` · ${error.stage}` : ""}`;
  const message = document.createElement("div");
  message.textContent = error.message || "Agent Server 未返回可读错误信息。";
  banner.append(title, message);
  if (error.details) {
    const details = document.createElement("pre");
    details.textContent = formatJson(error.details);
    banner.append(details);
  }
  return banner;
}

function buildTurnSection(turn, index) {
  const section = document.createElement("section");
  section.className = "response-turn";
  section.dataset.turnId = turn.localId;
  section.dataset.renderVersion = String(turn.renderVersion || 0);
  const heading = document.createElement("div");
  heading.className = "turn-heading";
  const label = document.createElement("strong");
  label.textContent = `第 ${index + 1} 次响应`;
  const metadata = document.createElement("span");
  metadata.textContent = [turn.responseId, turn.revision ? `revision ${turn.revision}` : null, turn.status]
    .filter(Boolean).join(" · ");
  heading.append(label, metadata);
  section.append(heading);
  if (turn.userText) section.append(messageRow("user", turn.userText));
  const activities = [...turn.activities.values()];
  if (activities.some((item) => !isCallableActivity(item))) section.append(renderActivityList(turn));
  if (activities.some(isCallableActivity)) section.append(renderCallableActivities(turn));
  if (turn.error) section.append(renderError(turn.error));
  for (const output of turn.outputs) {
    if (output.type === "reasoning" && output.value) {
      section.append(renderReasoning(output.value, `${turn.localId}:${output.key}`));
    }
    if (output.type === "text" && output.value) section.append(messageRow("assistant", output.value));
    if (output.type === "artifact") {
      section.append(renderArtifact(output.value, `${turn.localId}:${output.key}`));
    }
    if (output.type === "interrupt") section.append(renderInterrupt(output.value, turn));
  }
  return section;
}

function renderConversation() {
  const root = $("#conversation");
  if (!state.turns.length) {
    root.replaceChildren();
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "发送请求后按 Response 分段展示运行过程";
    root.append(empty);
    return;
  }
  for (const [index, turn] of state.turns.entries()) {
    const previous = root.children[index];
    const unchanged = previous?.dataset.turnId === turn.localId
      && previous.dataset.renderVersion === String(turn.renderVersion || 0);
    if (unchanged) continue;
    const section = buildTurnSection(turn, index);
    if (previous) previous.replaceWith(section);
    else root.append(section);
  }
  while (root.children.length > state.turns.length) root.lastElementChild.remove();
  root.scrollTop = root.scrollHeight;
}

function currentTrace() {
  return state.runId ? state.traceByRun.get(state.runId) : null;
}

function mergeDebugSnapshot(turn, debug) {
  if (!debug || !turn.runId) return;
  let traceRun = state.traceByRun.get(turn.runId);
  if (!traceRun) {
    traceRun = {runId: turn.runId, segments: [], llm_calls: [], nodes: [], tool_calls: [], fallbacks: [], errors: []};
    state.traceByRun.set(turn.runId, traceRun);
  }
  if (traceRun.segments.some((item) => item.responseId === turn.responseId)) return;
  const trace = debug.trace || debug;
  traceRun.segments.push({responseId: turn.responseId, trace});
  for (const key of ["llm_calls", "nodes", "tool_calls", "fallbacks", "errors"]) {
    traceRun[key].push(...(trace[key] || []));
  }
}

function groupedLlmCalls(rawCalls) {
  const calls = [];
  const indexed = new Map();
  for (const item of rawCalls) {
    const key = item.id || item.call_id || `${item.phase || "unknown"}-${calls.length}`;
    let call = indexed.get(key);
    if (!call) {
      call = {phase: item.phase || "unknown", provider_outputs: [], usage_items: []};
      calls.push(call);
      indexed.set(key, call);
    }
    if (item.provider_raw_output !== undefined) call.provider_outputs.push(item.provider_raw_output);
    if (item.usage !== undefined) call.usage_items.push(item.usage);
    Object.assign(call, item);
  }
  return calls;
}

function llmCallKey(call, index) {
  return String(call.id || call.call_id || `${call.phase || "unknown"}:${index}`);
}

function llmFieldValue(value) {
  if (value === undefined || value === null || value === "") return null;
  return formattedValue(value);
}

function createLlmField(label, value, className = "") {
  const field = document.createElement("section");
  field.className = `llm-field ${className}`.trim();
  const title = document.createElement("header");
  title.className = "llm-field-title";
  title.textContent = label;
  const pre = document.createElement("pre");
  pre.className = "llm-field-content";
  pre.textContent = formattedValue(value);
  field.append(title, pre);
  return field;
}

function llmFields(call) {
  return [
    ["system_prompt", "系统提示词", call.system_prompt, "prompt"],
    ["input", "原始输入", call.input, "large"],
    [
      "provider_output",
      "原始模型输出",
      call.provider_outputs.length ? call.provider_outputs : call.provider_raw_output,
      "large",
    ],
    ["structured_output", "结构化输出", call.raw_output, "large"],
    ["usage", "Usage", call.usage_items.length ? call.usage_items : undefined, "compact"],
    ["error", "错误", call.error, "compact error"],
  ].filter(([, , value]) => llmFieldValue(value) !== null);
}

function updateLlmCallCard(card, call, index) {
  const summary = card.querySelector(":scope > summary");
  summary.textContent = `${index + 1}. ${call.phase || "unknown"} · ${call.model || "model"} · ${call.status || "stream"}${call.thinking ? " · thinking" : ""}`;
  const body = card.querySelector(":scope > .llm-call-body");
  const fields = llmFields(call);
  const expected = new Set(fields.map(([key]) => key));

  for (const existing of body.querySelectorAll(":scope > .llm-field")) {
    if (!expected.has(existing.dataset.fieldKey)) existing.remove();
  }
  for (const [key, label, value, className] of fields) {
    const rendered = llmFieldValue(value);
    let field = body.querySelector(`:scope > .llm-field[data-field-key="${key}"]`);
    if (!field) {
      field = createLlmField(label, value, className);
      field.dataset.fieldKey = key;
      body.append(field);
    } else if (field.renderedValue !== rendered) {
      const content = field.querySelector(":scope > .llm-field-content");
      const scrollTop = content.scrollTop;
      const scrollLeft = content.scrollLeft;
      content.textContent = rendered;
      content.scrollTop = scrollTop;
      content.scrollLeft = scrollLeft;
    }
    field.renderedValue = rendered;
  }
}

function createLlmCallCard(call, index, key) {
  const details = document.createElement("details");
  details.className = "llm-call";
  details.dataset.callKey = key;
  restoreDisclosure(details, `llm:${state.runId}:${key}`);
  details.append(document.createElement("summary"));
  const body = document.createElement("div");
  body.className = "llm-call-body";
  details.append(body);
  updateLlmCallCard(details, call, index);
  return details;
}

function renderLlmTrace(calls) {
  const llmView = $("#llm-view");
  let list = llmView.querySelector(":scope > .llm-call-list");
  let empty = llmView.querySelector(":scope > .empty");

  if (!calls.length) {
    if (list) list.remove();
    if (!empty) {
      empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "每个 Response 终态后累计显示当前 run 的内部 LLM 调用";
      llmView.append(empty);
    }
    return;
  }

  if (empty) empty.remove();
  if (!list) {
    list = document.createElement("div");
    list.className = "llm-call-list";
    llmView.append(list);
  }

  const expected = new Set();
  for (const [index, call] of calls.entries()) {
    const key = llmCallKey(call, index);
    expected.add(key);
    let card = [...list.children].find((item) => item.dataset.callKey === key);
    if (!card) {
      card = createLlmCallCard(call, index, key);
      list.append(card);
    } else {
      updateLlmCallCard(card, call, index);
    }
  }
  for (const card of [...list.children]) {
    if (!expected.has(card.dataset.callKey)) card.remove();
  }
}

function toolCallKey(call, index) {
  return String(call.id || call.call_id || `${call.tool || call.skill || "unknown"}:${index}`);
}

function toolFields(call) {
  return [
    ["arguments", "调用参数", call.arguments, "large"],
    ["result", "调用结果", call.result, "large"],
    ["error", "错误", call.error, "compact error"],
  ].filter(([, , value]) => llmFieldValue(value) !== null);
}

function updateToolCallCard(card, call, index) {
  const summary = card.querySelector(":scope > summary");
  const type = call.kind === "skill" || call.tool_type === "skill" ? "Skill" : "工具";
  const name = call.skill || call.tool || call.name || "unknown";
  const endpoint = call.path ? ` · ${call.method || ""} ${call.path}` : "";
  summary.textContent = `${index + 1}. ${type} · ${name}${endpoint} · ${call.status || "unknown"}${call.elapsed_ms != null ? ` · ${formatElapsed(Number(call.elapsed_ms))}` : ""}`;
  const body = card.querySelector(":scope > .llm-call-body");
  const fields = toolFields(call);
  const expected = new Set(fields.map(([key]) => key));

  for (const existing of body.querySelectorAll(":scope > .llm-field")) {
    if (!expected.has(existing.dataset.fieldKey)) existing.remove();
  }
  for (const [key, label, value, className] of fields) {
    const rendered = llmFieldValue(value);
    let field = body.querySelector(`:scope > .llm-field[data-field-key="${key}"]`);
    if (!field) {
      field = createLlmField(label, value, className);
      field.dataset.fieldKey = key;
      body.append(field);
    } else if (field.renderedValue !== rendered) {
      const content = field.querySelector(":scope > .llm-field-content");
      const scrollTop = content.scrollTop;
      const scrollLeft = content.scrollLeft;
      content.textContent = rendered;
      content.scrollTop = scrollTop;
      content.scrollLeft = scrollLeft;
    }
    field.renderedValue = rendered;
  }
}

function createToolCallCard(call, index, key) {
  const details = document.createElement("details");
  details.className = "tool-call llm-call";
  details.dataset.callKey = key;
  restoreDisclosure(details, `tool:${state.runId}:${key}`);
  details.append(document.createElement("summary"));
  const body = document.createElement("div");
  body.className = "llm-call-body";
  details.append(body);
  updateToolCallCard(details, call, index);
  return details;
}

function renderToolTrace(calls) {
  const toolView = $("#tool-view");
  let list = toolView.querySelector(":scope > .tool-call-list");
  let empty = toolView.querySelector(":scope > .empty");

  if (!calls.length) {
    if (list) list.remove();
    if (!empty) {
      empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "每个 Response 终态后累计显示当前 run 的工具与 Skill 原始调用";
      toolView.append(empty);
    }
    return;
  }

  if (empty) empty.remove();
  if (!list) {
    list = document.createElement("div");
    list.className = "tool-call-list";
    toolView.append(list);
  }

  const expected = new Set();
  for (const [index, call] of calls.entries()) {
    const key = toolCallKey(call, index);
    expected.add(key);
    let card = [...list.children].find((item) => item.dataset.callKey === key);
    if (!card) {
      card = createToolCallCard(call, index, key);
      list.append(card);
    } else {
      updateToolCallCard(card, call, index);
    }
  }
  for (const card of [...list.children]) {
    if (!expected.has(card.dataset.callKey)) card.remove();
  }
}

function activeTraceView() {
  return document.querySelector(".trace-tab.active")?.dataset.tab || "timeline";
}

function boundedText(text, label) {
  if (text.length <= TRACE_VIEW_MAX_CHARS) return text;
  return `${label}过大，开发面板仅显示末尾 ${TRACE_VIEW_MAX_CHARS} 个字符。完整数据仍保留在内存中，可使用复制 Trace 导出。\n\n${text.slice(-TRACE_VIEW_MAX_CHARS)}`;
}

function boundedEventText(entries) {
  let size = 0;
  const selected = [];
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index];
    size += entry.length + 2;
    if (size > TRACE_VIEW_MAX_CHARS) break;
    selected.push(entry);
  }
  selected.reverse();
  const omitted = selected.length < entries.length
    ? `前 ${entries.length - selected.length} 条事件因显示上限被省略。完整事件仍保留在内存中。\n\n`
    : "";
  return omitted + selected.join("\n\n");
}

function renderTrace() {
  const traceRun = currentTrace();
  const activeView = activeTraceView();
  if (activeView === "timeline" && (state.timelineViewDirty || state.forceTraceViewRender)) {
    const timeline = $("#timeline-view");
    timeline.replaceChildren();
    const currentTurns = state.runId ? state.turns.filter((turn) => turn.runId === state.runId) : [];
    const activities = currentTurns.flatMap((turn) => [...turn.activities.values()]);
    for (const activity of activities) {
      const item = document.createElement("article");
      item.className = `timeline-item ${activity.status || ""}`;
      const title = document.createElement("strong");
      title.textContent = activity.label || activity.name;
      const detail = document.createElement("span");
      const duration = activity.status !== "running" ? formatElapsed(activity._elapsedMs) : "";
      detail.textContent = [activity.node || "workflow", activity.kind || "node", activity.status, duration]
        .filter(Boolean).join(" · ");
      item.append(title, detail);
      timeline.append(item);
    }
    if (!activities.length) timeline.innerHTML = '<p class="empty">运行后显示当前 run 的节点与工具活动</p>';
    state.timelineViewDirty = false;
  } else if (activeView === "llm" && (state.llmViewDirty || state.forceTraceViewRender)) {
    renderLlmTrace(groupedLlmCalls(traceRun?.llm_calls || []));
    state.llmViewDirty = false;
  } else if (activeView === "tool" && (state.toolViewDirty || state.forceTraceViewRender)) {
    renderToolTrace(traceRun?.tool_calls || []);
    state.toolViewDirty = false;
  } else if (activeView === "debug" && (state.debugViewDirty || state.forceTraceViewRender)) {
    const text = traceRun
      ? formatJson(traceRun.segments)
      : "终态后显示当前 run 的节点、LLM 和工具原始输入输出。";
    $("#debug-view").textContent = boundedText(text, "原始调用数据");
    state.debugViewDirty = false;
  } else if (activeView === "events" && (state.eventsViewDirty || state.forceTraceViewRender)) {
    const elapsed = performance.now() - lastEventsRenderAt;
    if (state.forceTraceViewRender || elapsed >= 500) {
      $("#events-view").textContent = boundedEventText(state.rawEvents);
      state.eventsViewDirty = false;
      lastEventsRenderAt = performance.now();
    } else if (eventsRenderTimer === null) {
      eventsRenderTimer = window.setTimeout(() => {
        eventsRenderTimer = null;
        requestRender({conversation: false});
      }, 500 - elapsed);
    }
  }
  state.forceTraceViewRender = false;
}

function updateRunMetadata(workflowStatus = "") {
  const parts = [
    state.runId ? `run ${state.runId}` : null,
    state.responseId ? `response ${state.responseId}` : null,
    state.revision ? `revision ${state.revision}` : null,
    workflowStatus || null,
  ].filter(Boolean);
  $("#run-metadata").textContent = parts.join(" · ") || "未运行";
  $("#cancel-run").disabled = !state.responseId || ["completed", "failed", "cancelled"].includes(workflowStatus);
}

function applyEvent(eventName, payload, raw) {
  const turn = state.currentTurn || createTurn({input: []});
  const entry = `${eventName}\n${raw}`;
  state.rawEvents.push(entry);
  turn.rawEvents.push(entry);
  if (state.rawEvents.length > 3000) state.rawEvents.shift();
  state.eventsViewDirty = true;
  if (eventName === "response.created") {
    state.timelineViewDirty = true;
    const response = payload.response || {};
    turn.responseId = response.id;
    turn.runId = response.metadata?.run_id || payload.run_id;
    turn.artifactId = response.metadata?.artifact_id;
    turn.revision = response.metadata?.revision;
    turn.status = response.metadata?.workflow_status || "running";
    state.responseId = turn.responseId;
    state.runId = turn.runId;
    state.artifactId = turn.artifactId;
    state.revision = turn.revision;
    updateRunMetadata(turn.status);
  } else if (eventName === "response.output_text.delta") {
    turn.assistantText += payload.delta || "";
    ensureOutput(turn, "text", "assistant", turn.assistantText);
  } else if (eventName === "response.reasoning_summary_text.delta") {
    turn.reasoningText += payload.delta || "";
    ensureOutput(turn, "reasoning", "reasoning", turn.reasoningText);
  } else if (eventName === "agent.activity") {
    const activity = payload.activity || {};
    if (activity.id) {
      const previous = turn.activities.get(activity.id);
      const observedAt = performance.now();
      const startedAt = previous?._startedAt ?? observedAt;
      const terminal = ["completed", "degraded", "cancelled", "failed"].includes(activity.status);
      turn.activities.set(activity.id, {
        ...previous,
        ...activity,
        _startedAt: startedAt,
        _elapsedMs: terminal ? (previous?._elapsedMs ?? observedAt - startedAt) : previous?._elapsedMs,
      });
    }
    state.timelineViewDirty = true;
  } else if (eventName === "agent.artifact") {
    const artifact = payload.artifact || {};
    const key = `${artifact.stage || "artifact"}:${artifact.id || ""}`;
    turn.artifacts.set(key, artifact);
    ensureOutput(turn, "artifact", key, artifact);
  } else if (eventName === "agent.interrupt") {
    turn.interrupt = payload.interrupt || null;
    if (turn.interrupt) ensureOutput(turn, "interrupt", turn.interrupt.id, turn.interrupt);
  } else if (eventName === "response.completed" || eventName === "response.failed") {
    const response = payload.response || {};
    turn.status = response.metadata?.workflow_status || response.status;
    turn.debug = payload.debug || turn.debug;
    turn.error = eventName === "response.failed" ? (response.error || payload.error || null) : null;
    mergeDebugSnapshot(turn, turn.debug);
    state.llmViewDirty = true;
    state.toolViewDirty = true;
    state.debugViewDirty = true;
    updateRunMetadata(turn.status);
    if (turn.assistantText) state.history.push({role: "assistant", content: turn.assistantText});
    if (eventName === "response.failed") setConnection("运行失败", "failed");
  }
  touchTurn(turn);
  requestRender();
}

async function readSse(response) {
  if (!response.ok) {
    const body = await response.text();
    let error;
    try {
      const payload = JSON.parse(body);
      error = payload.error || {code: `HTTP_${response.status}`, message: body};
    } catch (_) {
      error = {code: `HTTP_${response.status}`, message: body};
    }
    if (state.currentTurn) {
      state.currentTurn.error = error;
      state.currentTurn.status = "failed";
    }
    if (state.currentTurn) touchTurn(state.currentTurn);
    requestRender({immediate: true});
    throw new Error(`${error.code}: ${error.message}`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const {value, done} = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, {stream: true});
    const frames = buffer.split("\n\n");
    buffer = frames.pop() || "";
    for (const frame of frames) {
      let eventName = "message";
      const data = [];
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) eventName = line.slice(6).trim();
        if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
      }
      const raw = data.join("\n");
      if (!raw || raw === "[DONE]") continue;
      applyEvent(eventName, JSON.parse(raw), raw);
    }
  }
}

async function sendPayload(payload) {
  if (state.receiving) throw new Error("当前仍在接收 SSE，请先停止接收或等待终态。");
  createTurn(payload);
  state.controller = new AbortController();
  state.receiving = true;
  $("#stop-receiving").disabled = false;
  setConnection("运行中", "running");
  requestRender({immediate: true});
  try {
    const response = await fetch("/api/responses", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({agent_base_url: $("#agent-url").value.trim(), payload}),
      signal: state.controller.signal,
    });
    await readSse(response);
    if (!state.controller.signal.aborted && !$("#connection-state").classList.contains("failed")) {
      setConnection(activeInterruptTurn() ? "等待审批" : "已结束");
    }
  } catch (error) {
    if (error.name === "AbortError") {
      setConnection("已停止接收");
    } else {
      setConnection("请求错误", "failed");
      state.rawEvents.push(`client.error\n${String(error)}`);
      state.eventsViewDirty = true;
      requestRender({conversation: false, immediate: true});
    }
  } finally {
    state.receiving = false;
    state.controller = null;
    $("#stop-receiving").disabled = true;
    requestRender({immediate: true});
  }
}

async function submitEditedRequest() {
  const payload = JSON.parse($("#request-json").value);
  state.history = normalizeHistory(payload.input);
  resetRunViews();
  await sendPayload(payload);
}

async function submitMessage() {
  const text = $("#user-input").value.trim();
  if (!text) return;
  state.history.push({role: "user", content: text});
  $("#user-input").value = "";
  const payload = buildRequest();
  $("#request-json").value = formatJson(payload);
  await sendPayload(payload);
}

async function submitDecision(turn, decision, feedback, selection = null) {
  if (!turn.interrupt || turn.interrupt.status !== "pending" || !turn.responseId) return;
  if (decision === "revise" && !feedback && !selection) {
    setConnection("请填写修改反馈", "failed");
    return;
  }
  const artifactName = reviewStageLabel(turn.interrupt.stage);
  const selectedOption = selection
    ? turn.interrupt.form.options?.find((item) => item.id === selection.option_id)
    : null;
  const selectedValue = selection?.option_id === "custom" ? selection : selectedOption;
  const isIntentSelection = turn.interrupt.form?.clarification_type === "intent_confirmation";
  const isConflictSelection = turn.interrupt.form?.review_type === "material_conflict";
  const userText = selectedValue && isConflictSelection
    ? selectedValue.id === "custom_feedback"
      ? `已提交素材冲突处理意见：${feedback}`
      : `已选择素材冲突处理方式：${selectedValue.label || selectedValue.id}。`
    : selectedValue && isIntentSelection
    ? `已确认围绕“${selectedValue.topic}”生成微信公众号文章。`
    : selectedValue
    ? `已确认素材搜集方向：主题为“${selectedValue.target}”，重点关注“${selectedValue.about}”。`
    : decision === "approve"
    ? `已确认${artifactName}。`
    : decision === "regenerate"
      ? `请重新生成${artifactName}${feedback ? `：${feedback}` : "。"}`
      : feedback;
  turn.interrupt.status = "resolved";
  turn.interrupt.decision = decision;
  turn.interrupt.selection = selection;
  touchTurn(turn);
  state.history.push({role: "user", content: userText});
  const payload = buildRequest();
  payload.previous_response_id = turn.responseId;
  payload.context.hitl = {interrupt_id: turn.interrupt.id, decision, feedback};
  if (selection) payload.context.hitl.selection = selection;
  $("#request-json").value = formatJson(payload);
  requestRender({immediate: true});
  await sendPayload(payload);
}

function reviewStageLabel(stage) {
  return {
    intent_clarification: "公众号文章生成方向",
    docs_research_clarification: "素材搜集方向",
    material_conflict_review: "素材冲突处理方式",
    task_spec_review: "文章任务书",
    outline_review: "文章大纲",
    article_review: "未排版文章",
  }[stage] || "当前结果";
}

function decisionLabel(decision) {
  return {
    approve: "已接受",
    revise: "按反馈修改",
    regenerate: "完全重新生成",
  }[decision] || "已处理";
}

async function cancelRun() {
  if (!state.responseId) return;
  setConnection("正在取消", "running");
  const response = await fetch(`/api/responses/${encodeURIComponent(state.responseId)}/cancel`, {
    method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify({
      agent_base_url: $("#agent-url").value.trim(),
      session_id: $("#session-id").value.trim(),
    }),
  });
  const body = await response.json();
  state.rawEvents.push(`cancel ${response.status}\n${formatJson(body)}`);
  state.eventsViewDirty = true;
  if (response.ok) {
    if (state.currentTurn) {
      state.currentTurn.status = body.status || "cancelled";
      touchTurn(state.currentTurn);
    }
    for (const turn of state.turns) {
      if (turn.interrupt?.status === "pending") {
        turn.interrupt.status = "resolved";
        turn.interrupt.decision = "cancelled";
        touchTurn(turn);
      }
    }
    setConnection(response.status === 202 ? "取消确认中" : "已取消");
    updateRunMetadata(body.status || "cancelled");
    $("#cancel-run").disabled = true;
  } else {
    setConnection("取消失败", "failed");
  }
  requestRender({immediate: true});
}

async function selectRandomDocuments() {
  setConnection("读取文档中", "running");
  const response = await fetch("/api/random-documents", {
    method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify({
      manifest_file: $("#manifest-file").value.trim(),
      user_id: $("#user-id").value.trim(),
      kb_id: $("#kb-id").value.trim(),
      session_id: $("#session-id").value.trim(),
      count: Number($("#document-count").value),
      retrieval_base_url: $("#retrieval-url").value.trim(),
    }),
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || "随机文档失败");
  state.selectedDocuments = body.documents || [];
  renderDocuments();
  setConnection("文档已就绪");
}

function generateEditableRequest() {
  if (!state.history.length) {
    state.history = [{role: "user", content: "请根据这些参考文档生成一篇适合微信公众号发布的文章。"}];
  }
  $("#request-json").value = formatJson(buildRequest());
}

function resetRunViews() {
  state.turns = [];
  state.currentTurn = null;
  state.responseId = null;
  state.runId = null;
  state.artifactId = null;
  state.revision = null;
  state.rawEvents = [];
  state.traceByRun.clear();
  state.disclosureState.clear();
  state.timelineViewDirty = true;
  state.llmViewDirty = true;
  state.toolViewDirty = true;
  state.debugViewDirty = true;
  state.eventsViewDirty = true;
  state.forceTraceViewRender = true;
  requestRender({immediate: true});
}

function resetPanel() {
  if (state.controller) state.controller.abort();
  state.selectedDocuments = [];
  state.history = [];
  resetRunViews();
  $("#session-id").value = defaultSessionId();
  $("#request-json").value = "";
  renderDocuments();
  updateRunMetadata();
  setConnection("就绪");
}

async function initialize() {
  const response = await fetch("/api/config");
  const config = await response.json();
  $("#agent-url").value = config.agent_base_url;
  $("#retrieval-url").value = config.retrieval_base_url;
  $("#manifest-file").value = config.manifest_file;
  $("#user-id").value = config.user_id;
  $("#kb-id").value = config.kb_id;
  $("#session-id").value = config.session_id;
  renderDocuments();
  requestRender({immediate: true});
}

$("#random-documents").addEventListener("click", () => selectRandomDocuments().catch((error) => setConnection(String(error), "failed")));
$("#generate-request").addEventListener("click", generateEditableRequest);
$("#format-request").addEventListener("click", () => { $("#request-json").value = formatJson(JSON.parse($("#request-json").value)); });
$("#send-request").addEventListener("click", () => submitEditedRequest().catch((error) => setConnection(String(error), "failed")));
$("#send-message").addEventListener("click", () => submitMessage().catch((error) => setConnection(String(error), "failed")));
$("#stop-receiving").addEventListener("click", () => state.controller?.abort());
$("#cancel-run").addEventListener("click", () => cancelRun().catch((error) => setConnection(String(error), "failed")));
$("#clear-button").addEventListener("click", resetPanel);
$("#copy-trace").addEventListener("click", () => navigator.clipboard.writeText(formatJson(currentTrace() || {})));
for (const tab of document.querySelectorAll(".trace-tab")) {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".trace-tab").forEach((item) => item.classList.remove("active"));
    document.querySelectorAll(".trace-view").forEach((item) => {
      item.classList.remove("active");
      item.hidden = true;
    });
    tab.classList.add("active");
    const view = $(`#${tab.dataset.tab}-view`);
    view.hidden = false;
    view.classList.add("active");
    state.forceTraceViewRender = true;
    requestRender({conversation: false, immediate: true});
  });
}

initialize().catch((error) => setConnection(String(error), "failed"));
