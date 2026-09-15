from __future__ import annotations

import asyncio
import inspect
from time import monotonic
from typing import Any, Literal, cast

from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt

from app.agent_engine import AgentBudget
from app.core.errors import AppError
from app.core.ids import prefixed_id
from app.core.recovery import error_with_recovery
from app.core.requests import is_pure_continue
from app.debug.trace import current as current_debug_trace
from app.debug.trace import emit_snapshot, get_or_create, record, use_collector
from app.events.internal import activity, artifact, emit, new_activity_id, public_text
from app.graph.state import RuntimeContext, WechatArticleState
from app.integrations.retrieval import raw_text
from app.llm.inputs import (
    ArticleGenerationInput,
    CompactionChunkInput,
    ConflictCheckInput,
    ConflictChunkInput,
    ConflictHandlerGroupInput,
    ConflictHandlerInput,
    GenerationBasis,
    ImagePlanInput,
    ImagePositionInput,
    IntentInput,
    MaterialCompactionInput,
    MaterialFilterInput,
    MaterialFilterItemInput,
    MaterialSufficiencyInput,
    OrchestratorInput,
    OutlineGenerationInput,
    QueryPlannerInput,
    ResearchDirectionInput,
    ResearchDirectionPlanInput,
    ResearchDirectionValidationInput,
    SessionMemoryInput,
    TaskSpecGenerationInput,
    WebInfoOverviewInput,
    WebMaterialInput,
    clean_conversation,
    clean_document,
    clean_documents,
    clean_material_evidence,
    clean_material_summaries,
    clean_task_spec,
    outline_markdown,
    render_input,
    require_task_spec,
)
from app.llm.schemas import (
    ConflictCheckOutput,
    ImagePlanOutput,
    IntentOutput,
    MarkdownOutput,
    MaterialSufficiencyOutput,
    OrchestratorOutput,
    QueryPlanOutput,
    ResearchDirectionPlanOutput,
    ResearchDirectionValidationOutput,
    SessionMemoryOutput,
    TaskSpecOutput,
    WebMaterialOutput,
    dynamic_boolean_output,
    dynamic_string_output,
)
from app.materials.processing import (
    apply_conflict_notes,
    conflict_chunks,
    consumable_materials,
    estimated_material_tokens,
    material_source_names,
    normalize_material_library,
    replace_reference_chunks_with_summary,
    safe_material_token_budget,
    trim_materials_to_budget,
    web_search_materials,
)
from app.prompts.system import (
    ARTICLE_SYSTEM_PROMPT,
    CONFLICT_CHECK_SYSTEM_PROMPT,
    CONFLICT_HANDLER_SYSTEM_PROMPT,
    IMAGE_PLAN_SYSTEM_PROMPT,
    INTENT_ROUTER_SYSTEM_PROMPT,
    MATERIAL_COMPACTION_SYSTEM_PROMPT,
    MATERIAL_FILTER_SYSTEM_PROMPT,
    MATERIAL_SUFFICIENCY_SYSTEM_PROMPT,
    ORCHESTRATOR_SYSTEM_PROMPT,
    OUTLINE_SYSTEM_PROMPT,
    QUERY_PLANNER_SYSTEM_PROMPT,
    RESEARCH_DIRECTION_SYSTEM_PROMPT,
    RESEARCH_DIRECTION_VALIDATION_SYSTEM_PROMPT,
    SESSION_MEMORY_SYSTEM_PROMPT,
    TASK_SPEC_SYSTEM_PROMPT,
    WEB_INFO_OVERVIEW_SYSTEM_PROMPT,
    WEB_MATERIAL_SYSTEM_PROMPT,
)
from app.rendering.skill_driven.adapter import run_layout_agent
from app.rendering.wechat_layout import LayoutEngine
from app.rendering.wechat_layout.catalog import describe_supported_themes
from app.rendering.wechat_layout.contracts import LayoutEvent
from app.rendering.wechat_layout.errors import LayoutError
from app.rendering.wechat_layout.llm_selector import select_theme_with_llm
from app.rendering.wechat_layout.themes.registry import default_registry
from app.runtime.services import get_graph_services

EMPTY_MEMORY = {
    "docs_research": "",
    "task_spec": "",
    "outline": "",
    "article": "",
    "ai_image": "",
    "html_layout": "",
}


def _generation_basis(materials: list[dict[str, Any]] | None) -> GenerationBasis:
    return "reference_materials" if consumable_materials(materials) else "general_knowledge"


async def _model_event(kind: str, data: dict[str, Any]) -> None:
    if kind == "reasoning_delta":
        emit("reasoning_delta", data)
    elif kind == "usage":
        record(
            "llm_calls",
            {"id": data.get("call_id"), "phase": data.get("phase"), "usage": data},
        )
        emit("usage", data)
    elif kind == "structured_repair":
        emit(
            "warning",
            {
                "code": "STRUCTURED_OUTPUT_REPAIRED",
                "message": "模型输出未通过严格校验，正在按同一 schema 修复后继续。",
                "phase": data.get("phase"),
                "attempt": data.get("attempt"),
            },
        )
    elif kind == "structured_retry":
        emit(
            "warning",
            {
                "code": "STRUCTURED_OUTPUT_RETRYING",
                "message": "模型输出未通过严格校验，正在丢弃本次结果并重新执行。",
                "phase": data.get("phase"),
                "attempt": data.get("attempt"),
            },
        )


def _context(runtime: Runtime[RuntimeContext]) -> RuntimeContext:
    return runtime.context or {}


def _research_direction_text(direction: dict[str, str]) -> str:
    return f"素材搜集主题：{direction['target']}\n素材关注方面：{direction['about']}"


def _parse_research_direction_resume(resume: dict[str, Any], options: list[dict[str, str]]) -> dict[str, str]:
    selection = resume.get("selection")
    if isinstance(selection, dict):
        raw_option_id = str(selection.get("option_id") or "").strip()
        option_id = "custom" if raw_option_id.lower() == "custom" else raw_option_id.upper()
        custom_about = str(selection.get("about") or "").strip()
        custom_target = str(selection.get("target") or "").strip()
    else:
        option_id = ""
        custom_about = ""
        custom_target = ""
    if option_id == "custom" and custom_about and custom_target:
        return {"option_id": "custom", "about": custom_about, "target": custom_target}
    if option_id and option_id != "custom":
        selected = next((item for item in options if item.get("id") == option_id), None)
        if selected is None:
            raise ValueError("selected research direction option is invalid")
        return {
            "option_id": option_id,
            "about": str(selected["about"]).strip(),
            "target": str(selected["target"]).strip(),
        }
    raise ValueError("research direction selection or custom about and target are required")


def _parse_intent_topic_resume(resume: dict[str, Any], options: list[dict[str, str]]) -> dict[str, str]:
    selection = resume.get("selection")
    if not isinstance(selection, dict):
        raise ValueError("intent topic selection is required")
    raw_option_id = str(selection.get("option_id") or "").strip()
    option_id = "custom" if raw_option_id.lower() == "custom" else raw_option_id.upper()
    if option_id == "custom":
        topic = str(selection.get("topic") or "").strip()
        if not topic:
            raise ValueError("custom intent topic is required")
        return {"option_id": "custom", "topic": topic}
    selected = next((item for item in options if item.get("id") == option_id), None)
    if selected is None:
        raise ValueError("selected intent topic option is invalid")
    return {"option_id": option_id, "topic": str(selected["topic"]).strip()}


async def intent_router(state: WechatArticleState, runtime: Runtime[RuntimeContext]) -> WechatArticleState:
    services = await get_graph_services()
    if state.get("has_prior_article_revision") and is_pure_continue(state["current_user_input"]):
        return {"intent": "wechat_article", "intent_topic_options": []}
    try:
        result = await services.llm.structured(
            run_id=state["run_id"],
            phase="intent_router",
            system_prompt=INTENT_ROUTER_SYSTEM_PROMPT,
            input_text=render_input(
                IntentInput(
                    conversation=clean_conversation(_context(runtime).get("conversation", [])),
                    latest_user_input=state["current_user_input"],
                    has_prior_article_revision=state.get("has_prior_article_revision", False),
                    previous_revision_status=state.get("previous_revision_status", ""),
                )
            ),
            output_model=IntentOutput,
            event_callback=_model_event,
        )
        is_wechat_article = result.is_wechat_article_intent
    except AppError:
        text = state["current_user_input"]
        if any(word in text for word in ("文章", "公众号", "写一篇")):
            return {"intent": "wechat_article"}
        raise
    if is_wechat_article:
        return {"intent": "wechat_article", "intent_topic_options": []}
    if state.get("intent_clarification_used"):
        return {"intent": "other", "intent_topic_options": []}
    public_text(result.user_facing_message)
    await services.artifacts.update_stage(
        state["artifact_id"],
        response_id=state["response_id"],
        current_stage="intent_clarification",
        status="waiting_for_input",
    )
    return {
        "intent": "needs_clarification",
        "intent_topic_options": [item.model_dump() for item in result.options],
        "intent_clarification_used": True,
        "pending_interrupt_id": prefixed_id("int"),
        "status": "needs_clarification",
    }


def route_intent(state: WechatArticleState) -> str:
    if state.get("intent") == "wechat_article":
        return "wechat"
    if state.get("intent") == "needs_clarification":
        return "clarify"
    return "other"


def intent_clarification_interrupt(state: WechatArticleState) -> Command[Any]:
    resume = interrupt(
        {
            "interrupt_id": state["pending_interrupt_id"],
            "response_id": state["response_id"],
            "stage": "intent_clarification",
            "artifact_id": state["artifact_id"],
            "revision": state["revision"],
            "form": {
                "form_type": "agent_clarification",
                "clarification_type": "intent_confirmation",
                "title": "确认公众号文章生成方向",
                "description": "请选择一个建议主题，或填写其他主题。",
                "selection_mode": "single",
                "options": state.get("intent_topic_options", []),
                "custom_option": {"enabled": True, "topic_label": "其他文章主题"},
                "fields": [],
            },
        }
    )
    if not isinstance(resume, dict):
        raise ValueError("intent clarification resume must be an object")
    if resume.get("type") == "cancel":
        return Command(update={"status": "cancelled"}, goto="cancelled")
    if resume.get("type") == "supersede":
        return Command(update={"status": "superseded"}, goto="superseded")
    selection = _parse_intent_topic_resume(resume, state.get("intent_topic_options", []))
    confirmation = f"用户已确认：围绕“{selection['topic']}”生成微信公众号文章。"
    current_user_input = f"{state['current_user_input']}\n\n{confirmation}"
    common_update = {
        "response_id": str(resume["response_id"]),
        "current_user_input": current_user_input,
        "intent_topic_options": [],
        "pending_interrupt_id": "",
        "status": "running",
    }
    if selection["option_id"] == "custom":
        custom_input = (
            f"{state['current_user_input']}\n\n用户在公众号文章意图确认卡中补充：{selection['topic']}"
        )
        return Command(
            update={**common_update, "current_user_input": custom_input, "intent": ""},
            goto="intent_router",
        )
    return Command(
        update={**common_update, "intent": "wechat_article"},
        goto="start_planning",
    )


async def non_wechat_response(state: WechatArticleState) -> WechatArticleState:
    services = await get_graph_services()
    await services.artifacts.mark_terminal(
        state["artifact_id"],
        response_id=state["response_id"],
        status="completed",
        current_stage="non_wechat",
    )
    public_text("这个请求不属于微信公众号文章生成，请切换到对应的 Agent 继续处理。")
    return {"status": "completed", "current_stage": "non_wechat"}


def start_planning(_: WechatArticleState) -> WechatArticleState:
    return {}


async def session_memory(state: WechatArticleState, runtime: Runtime[RuntimeContext]) -> WechatArticleState:
    services = await get_graph_services()
    try:
        result = await services.llm.structured(
            run_id=state["run_id"],
            phase="session_memory",
            system_prompt=SESSION_MEMORY_SYSTEM_PROMPT,
            input_text=render_input(
                SessionMemoryInput(
                    conversation=clean_conversation(_context(runtime).get("compact_history", []))
                )
            ),
            output_model=SessionMemoryOutput,
            event_callback=_model_event,
        )
        memory = result.model_dump()
        status: Literal["completed", "degraded"] = "completed"
    except AppError:
        memory = dict(EMPTY_MEMORY)
        status = "degraded"
    if _context(runtime).get("debug_enabled"):
        artifact(
            artifact_id=state["artifact_id"],
            revision=state["revision"],
            stage="session_memory",
            status=status,
            content=memory,
            summary={"non_empty_sections": sum(bool(value) for value in memory.values())},
        )
    return {"session_memory": memory}


async def orchestrator(state: WechatArticleState, runtime: Runtime[RuntimeContext]) -> WechatArticleState:
    services = await get_graph_services()
    current = await services.artifacts.get(state["artifact_id"])
    if current is None:
        raise AppError(404, "ARTIFACT_NOT_FOUND", "Artifact does not exist.")
    parent = await services.artifacts.nearest_approved_ancestor(current)
    target: Literal["docs_research", "task_spec", "outline", "article"]
    if parent is None:
        target = "docs_research"
        if state.get("doc_ids") or state.get("temp_doc_ids"):
            reason = "这是第一次生成文章，我会先研读您提供的参考文档。"
        else:
            reason = "这是第一次生成文章，我会先确认内容方向并调研可用素材。"
    else:
        try:
            result = await services.llm.structured(
                run_id=state["run_id"],
                phase="orchestrator",
                system_prompt=ORCHESTRATOR_SYSTEM_PROMPT,
                input_text=render_input(
                    OrchestratorInput(
                        conversation=clean_conversation(_context(runtime).get("conversation", [])),
                        latest_user_input=state["current_user_input"],
                        previous_revision_status=state.get("previous_revision_status", ""),
                        document_scope_changed=(
                            state["doc_ids"] != parent.doc_ids or state["temp_doc_ids"] != parent.temp_doc_ids
                        ),
                        approved_through_stage=parent.approved_through_stage or "",
                        task_spec=clean_task_spec(parent.task_spec),
                        outline_markdown=outline_markdown(parent.outline),
                    )
                ),
                output_model=OrchestratorOutput,
                event_callback=_model_event,
            )
            target, reason = result.target, result.reason
        except AppError:
            target, reason = (
                "docs_research",
                "路由判断暂时不可用；为避免遗漏需求变化，我会从文档研读重新开始。",
            )
    public_text(reason)
    return {"entry_stage": target, "route_reason": reason}


async def entry_dispatch(state: WechatArticleState) -> WechatArticleState:
    services = await get_graph_services()
    await services.artifacts.initialize_entry_stage(
        state["artifact_id"],
        response_id=state["response_id"],
        entry_stage=state["entry_stage"],
    )
    return {"current_stage": state["entry_stage"]}


def route_entry(state: WechatArticleState) -> str:
    return state["entry_stage"]


async def load_document_meta(state: WechatArticleState) -> WechatArticleState:
    services = await get_graph_services()
    if not state["doc_ids"] and not state["temp_doc_ids"]:
        return {"current_stage": "material_sufficiency", "document_meta": []}
    call_id = new_activity_id("document_meta")
    activity(
        activity_id=call_id,
        kind="tool",
        name="get_document_meta",
        label="读取参考文档信息",
        status="running",
        node="docs_research",
        input_summary={"document_count": len(state["doc_ids"]) + len(state["temp_doc_ids"])},
    )
    result = await services.retrieval.meta(
        user_id=state["user_id"],
        kb_id=state["kb_id"],
        session_id=state["session_id"],
        doc_ids=state["doc_ids"],
        temp_doc_ids=state["temp_doc_ids"],
    )
    activity(
        activity_id=call_id,
        kind="tool",
        name="get_document_meta",
        label="读取参考文档信息",
        status="completed",
        node="docs_research",
        output_summary={"document_count": len(result.get("documents") or [])},
    )
    return {
        "current_stage": "material_sufficiency",
        "document_meta": result.get("documents", []),
    }


async def material_sufficiency_judge(state: WechatArticleState) -> WechatArticleState:
    services = await get_graph_services()
    if not services.settings.web_search_enabled:
        public_text("当前联网素材搜索未开启，我将使用您提供的文档；文档不足时以通用知识补充。")
        return {"need_web_search": False, "web_info_overview": ""}
    if not state.get("document_meta"):
        public_text("本轮没有可用参考文档，我将尝试通过联网搜索补充素材。")
        return {"need_web_search": True}
    try:
        result = await services.llm.structured(
            run_id=state["run_id"],
            phase="material_sufficiency",
            system_prompt=MATERIAL_SUFFICIENCY_SYSTEM_PROMPT,
            input_text=render_input(
                MaterialSufficiencyInput(
                    user_request=state["current_user_input"],
                    documents=clean_documents(state.get("document_meta", [])),
                )
            ),
            output_model=MaterialSufficiencyOutput,
            event_callback=_model_event,
        )
    except AppError as exc:
        emit(
            "warning",
            {
                "code": "MATERIAL_SUFFICIENCY_DEGRADED",
                "message": "联网素材判断暂不可用，已保守使用文档素材或通用知识继续。",
                "error": exc.code,
            },
        )
        return {"need_web_search": False, "web_info_overview": ""}
    public_text(result.user_facing_message)
    return {"need_web_search": result.need_web_search}


def route_web_info_overview(state: WechatArticleState) -> str:
    return "search" if state.get("need_web_search") else "skip"


async def web_info_overview(state: WechatArticleState) -> WechatArticleState:
    services = await get_graph_services()
    call_id = new_activity_id("web_info_overview")
    activity(
        activity_id=call_id,
        kind="tool",
        name="ark.web_search",
        label="搜索主题背景",
        status="running",
        node="web_info_overview",
        input_summary={"purpose": "topic_background"},
    )
    try:
        result = await services.llm.web_search(
            run_id=state["run_id"],
            phase="web_info_overview",
            system_prompt=WEB_INFO_OVERVIEW_SYSTEM_PROMPT,
            input_text=render_input(WebInfoOverviewInput(user_request=state["current_user_input"])),
            max_keyword=2,
            event_callback=_model_event,
        )
    except AppError as exc:
        activity(
            activity_id=call_id,
            kind="tool",
            name="ark.web_search",
            label="主题背景搜索暂不可用",
            status="degraded",
            node="web_info_overview",
            error={"code": exc.code, "message": exc.message},
        )
        public_text("联网背景搜索暂不可用，我将继续根据现有信息确认素材方向。")
        return {"web_info_overview": ""}
    overview = str(result.output).strip()
    activity(
        activity_id=call_id,
        kind="tool",
        name="ark.web_search",
        label="主题背景搜索完成",
        status="completed",
        node="web_info_overview",
        output_summary={"query_count": len(result.queries)},
    )
    return {"web_info_overview": overview}


async def skip_web_info_overview(state: WechatArticleState) -> WechatArticleState:
    del state
    return {"web_info_overview": ""}


async def plan_research_direction(state: WechatArticleState) -> WechatArticleState:
    services = await get_graph_services()
    result = await services.llm.structured(
        run_id=state["run_id"],
        phase="research_direction",
        system_prompt=RESEARCH_DIRECTION_SYSTEM_PROMPT,
        input_text=render_input(
            ResearchDirectionPlanInput(
                user_request=state["current_user_input"],
                memory=state.get("session_memory", {}).get("docs_research", ""),
                documents=clean_documents(state.get("document_meta", [])),
                web_info_overview=state.get("web_info_overview", ""),
            )
        ),
        output_model=ResearchDirectionPlanOutput,
        event_callback=_model_event,
    )
    public_text(result.user_facing_message)
    await services.artifacts.update_stage(
        state["artifact_id"],
        response_id=state["response_id"],
        current_stage="docs_research_clarification",
        status="waiting_for_input",
    )
    return {
        "status": "needs_clarification",
        "document_coverage_mode": (result.coverage_mode if state.get("document_meta") else "best_effort"),
        "research_direction_options": [item.model_dump() for item in result.options],
        "pending_interrupt_id": prefixed_id("int"),
    }


def clarification_interrupt(state: WechatArticleState) -> Command[Any]:
    interrupt_id = state["pending_interrupt_id"]
    resume = interrupt(
        {
            "interrupt_id": interrupt_id,
            "response_id": state["response_id"],
            "stage": "docs_research_clarification",
            "artifact_id": state["artifact_id"],
            "revision": state["revision"],
            "form": {
                "form_type": "agent_clarification",
                "clarification_type": "research_direction",
                "title": "请确认素材搜集方向",
                "description": "请选择一个素材方向，或填写自定义的搜集主题与关注方面。",
                "selection_mode": "single",
                "options": state.get("research_direction_options", []),
                "custom_option": {
                    "enabled": True,
                    "about_label": "素材关注方面",
                    "target_label": "素材搜集主题",
                },
                "fields": [],
            },
        }
    )
    if not isinstance(resume, dict):
        raise ValueError("clarification resume must be an object")
    if resume.get("type") == "cancel":
        return Command(update={"status": "cancelled"}, goto="cancelled")
    if resume.get("type") == "supersede":
        return Command(update={"status": "superseded"}, goto="superseded")
    direction = _parse_research_direction_resume(resume, state.get("research_direction_options", []))
    is_custom = direction["option_id"] == "custom"
    return Command(
        update={
            "response_id": str(resume["response_id"]),
            "research_direction": direction,
            "confirmed_requirements": _research_direction_text(direction),
            "pending_interrupt_id": "",
            "clarification_round": state.get("clarification_round", 0) + (1 if is_custom else 0),
            "status": "validating_custom_direction" if is_custom else "research_ready",
        },
        goto="validate_custom_research_direction" if is_custom else "persist_research_direction",
    )


async def validate_custom_research_direction(state: WechatArticleState) -> WechatArticleState:
    services = await get_graph_services()
    if state.get("clarification_round", 0) >= services.settings.clarification_max_rounds:
        public_text("已达到研读方向确认轮次上限，我会采用您最后提交的方向开始研读。")
        return {"status": "research_ready"}
    result = await services.llm.structured(
        run_id=state["run_id"],
        phase="research_direction_validation",
        system_prompt=RESEARCH_DIRECTION_VALIDATION_SYSTEM_PROMPT,
        input_text=render_input(
            ResearchDirectionValidationInput(
                custom_direction=ResearchDirectionInput.model_validate(state["research_direction"]),
                user_request=state["current_user_input"],
                memory=state.get("session_memory", {}).get("docs_research", ""),
                documents=clean_documents(state.get("document_meta", [])),
                web_info_overview=state.get("web_info_overview", ""),
                clarification_round=state.get("clarification_round", 0),
                max_rounds=services.settings.clarification_max_rounds,
            )
        ),
        output_model=ResearchDirectionValidationOutput,
        event_callback=_model_event,
    )
    if result.sufficient:
        direction = {
            "option_id": "custom",
            "about": result.normalized_about,
            "target": result.normalized_target,
        }
        public_text(result.user_facing_message)
        return {
            "research_direction": direction,
            "confirmed_requirements": _research_direction_text(direction),
            "status": "research_ready",
        }
    public_text(result.user_facing_message)
    await services.artifacts.update_stage(
        state["artifact_id"],
        response_id=state["response_id"],
        current_stage="docs_research_clarification",
        status="waiting_for_input",
    )
    return {
        "status": "needs_clarification",
        "research_direction_options": [item.model_dump() for item in result.options],
        "pending_interrupt_id": prefixed_id("int"),
    }


def route_custom_direction_validation(state: WechatArticleState) -> str:
    return "ready" if state.get("status") == "research_ready" else "clarify"


async def persist_research_direction(state: WechatArticleState) -> WechatArticleState:
    services = await get_graph_services()
    await services.artifacts.update_stage(
        state["artifact_id"],
        response_id=state["response_id"],
        current_stage="docs_research",
        values={"research_direction": state["research_direction"]},
    )
    return {"current_stage": "docs_research"}


async def route_documents(state: WechatArticleState) -> WechatArticleState:
    services = await get_graph_services()
    if not state["doc_ids"] and not state["temp_doc_ids"]:
        return {
            "relevant_doc_ids": [],
            "document_coverage_mode": "best_effort",
            "retrieval_warnings": [
                {
                    "code": "NO_REFERENCE_DOCUMENTS",
                    "message": "本轮没有参考文档，将基于通用知识继续生成。",
                }
            ],
        }
    call_id = new_activity_id("route_documents")
    activity(
        activity_id=call_id,
        kind="tool",
        name="retrieval.route_documents",
        label="正在检索相关文档",
        status="running",
        node="docs_research",
        input_summary={"document_count": len(state["doc_ids"]) + len(state["temp_doc_ids"])},
    )
    selected, warnings = await services.retrieval.route(
        user_id=state["user_id"],
        kb_id=state["kb_id"],
        session_id=state["session_id"],
        doc_ids=state["doc_ids"],
        temp_doc_ids=state["temp_doc_ids"],
        topic=_research_direction_text(state["research_direction"]),
    )
    if not selected:
        warnings = [
            *warnings,
            {
                "code": "NO_RELEVANT_DOCUMENTS",
                "message": "没有参考文档符合本轮研读方向，将基于通用知识继续生成。",
            },
        ]
    activity(
        activity_id=call_id,
        kind="tool",
        name="retrieval.route_documents",
        label="相关文档检索完成",
        status="degraded" if warnings or not selected else "completed",
        node="docs_research",
        output_summary={"selected_count": len(selected), "warning_count": len(warnings)},
    )
    return {
        "relevant_doc_ids": selected,
        "document_coverage_mode": (state["document_coverage_mode"] if selected else "best_effort"),
        "retrieval_warnings": warnings,
    }


async def expired_material_filter(state: WechatArticleState) -> WechatArticleState:
    services = await get_graph_services()
    routed = await route_documents(state)
    current = await _require_artifact(state)
    ancestor = await services.artifacts.nearest_approved_ancestor(current)
    old_materials = normalize_material_library(
        ancestor.material_library if ancestor else [], documents=state.get("document_meta", [])
    )
    relevant = set(routed.get("relevant_doc_ids", []))
    candidates = [
        item
        for item in old_materials
        if item.get("material_kind") != "user_document" or item.get("source") in relevant
    ]
    if candidates:
        output_model = dynamic_boolean_output(
            "MaterialFilterDecision",
            [str(item["material_id"]) for item in candidates],
        )
        try:
            decision = await services.llm.structured(
                run_id=state["run_id"],
                phase="material_filter",
                system_prompt=MATERIAL_FILTER_SYSTEM_PROMPT,
                input_text=render_input(
                    MaterialFilterInput(
                        research_direction=ResearchDirectionInput.model_validate(state["research_direction"]),
                        materials=[
                            MaterialFilterItemInput(
                                material_id=str(item["material_id"]),
                                material_kind=item.get("material_kind", "user_document"),
                                summary=str(item.get("summary") or ""),
                            )
                            for item in candidates
                        ],
                    )
                ),
                output_model=output_model,
                event_callback=_model_event,
            )
            keep = decision.model_dump()
            candidates = [item for item in candidates if keep.get(str(item["material_id"]), True)]
        except AppError as exc:
            emit(
                "warning",
                {
                    "code": "MATERIAL_FILTER_DEGRADED",
                    "message": "旧素材筛选暂不可用，已保守保留仍属于相关文档的素材。",
                    "error": exc.code,
                },
            )
    await services.artifacts.update_stage(
        state["artifact_id"],
        response_id=state["response_id"],
        current_stage="material_workers",
        values={
            "relevant_doc_ids": routed.get("relevant_doc_ids", []),
            "document_coverage_mode": routed.get("document_coverage_mode", "best_effort"),
            "material_library": candidates,
        },
    )
    return {**routed, "current_stage": "material_workers"}


async def document_material_worker(state: WechatArticleState) -> WechatArticleState:
    services = await get_graph_services()
    meta_by_id = {item["doc_id"]: item for item in state.get("document_meta", [])}
    current = await _require_artifact(state)
    covered_doc_ids = {
        str(item.get("source") or item.get("source_doc_id") or "")
        for item in current.material_library or []
        if item.get("material_kind", "user_document") == "user_document"
    }
    pending_doc_ids = [doc_id for doc_id in state["relevant_doc_ids"] if doc_id not in covered_doc_ids]
    materials: list[dict[str, Any]] = []
    failures: list[str] = []
    technical_failures: list[str] = []

    async def extract(doc_id: str) -> list[dict[str, Any]]:
        call_id = new_activity_id("extract_document")
        activity(
            activity_id=call_id,
            kind="tool",
            name="get_document_raw",
            label=f"研读文档：{meta_by_id.get(doc_id, {}).get('doc_name', doc_id)}",
            status="running",
            node="docs_research",
        )
        try:
            raw = await services.retrieval.raw(user_id=state["user_id"], kb_id=state["kb_id"], doc_id=doc_id)
            text = raw_text(raw)
            if text and len(text) <= services.settings.short_document_max_chars:
                result = [
                    {
                        "material_id": prefixed_id("mat"),
                        "material_kind": "user_document",
                        "source": doc_id,
                        "summary": str(raw.get("doc_description") or raw.get("doc_name") or doc_id),
                        "active": True,
                        "metadata": {
                            "extraction_mode": "raw",
                            "queries": [],
                            "coverage_complete": True,
                            "warnings": [],
                        },
                        "orig_chunks": [
                            {
                                "chunk_id": f"{doc_id}:full",
                                "type": "full_text",
                                "page_number": None,
                                "path": f"{meta_by_id.get(doc_id, {}).get('doc_name', doc_id)}：全文",
                                "ref": doc_id,
                                "title": str(meta_by_id.get(doc_id, {}).get("doc_name") or doc_id),
                                "content": text,
                            }
                        ],
                    }
                ]
                activity(
                    activity_id=call_id,
                    kind="tool",
                    name="get_document_raw",
                    label="读取完整文档",
                    status="completed",
                    node="docs_research",
                    output_summary={"mode": "raw", "characters": len(text)},
                )
                return result
        except AppError:
            text = ""

        try:
            plan = await services.llm.structured(
                run_id=state["run_id"],
                phase="material_query_plan",
                system_prompt=QUERY_PLANNER_SYSTEM_PROMPT,
                input_text=render_input(
                    QueryPlannerInput(
                        document=clean_document(meta_by_id.get(doc_id), fallback_id=doc_id),
                        research_direction=ResearchDirectionInput.model_validate(state["research_direction"]),
                        group_count=services.settings.material_query_groups_per_document,
                    )
                ),
                output_model=QueryPlanOutput,
                event_callback=_model_event,
            )
            groups = [group.queries for group in plan.query_groups]
        except AppError:
            groups = [
                [f"请详细总结这篇文档中与以下文章需求相关的事实、数据和结构：{state['current_user_input']}"]
            ]
        output: list[dict[str, Any]] = []
        try:
            for queries in groups[: services.settings.material_query_groups_per_document]:
                retrieved = await services.retrieval.retrieve(
                    user_id=state["user_id"],
                    kb_id=state["kb_id"],
                    session_id=state["session_id"],
                    doc_ids=[doc_id] if doc_id in state["doc_ids"] else [],
                    temp_doc_ids=[doc_id] if doc_id in state["temp_doc_ids"] else [],
                    queries=queries,
                    ensure_document_coverage=True,
                    debug=False,
                )
                chunks = retrieved.get("chunks") or []
                if chunks:
                    output.append(
                        {
                            "material_id": prefixed_id("mat"),
                            "material_kind": "user_document",
                            "source": doc_id,
                            "summary": "；".join(queries),
                            "active": True,
                            "metadata": {
                                "extraction_mode": "retrieve",
                                "queries": queries,
                                "coverage_complete": bool((retrieved.get("coverage") or {}).get("complete")),
                                "warnings": retrieved.get("warnings") or [],
                            },
                            "orig_chunks": chunks,
                        }
                    )
        except Exception as exc:
            activity(
                activity_id=call_id,
                kind="tool",
                name="retrieve_document_materials",
                label="提取文档素材失败",
                status="failed",
                node="docs_research",
                error={
                    "code": getattr(exc, "code", type(exc).__name__),
                    "message": str(getattr(exc, "message", exc)),
                },
            )
            raise
        activity(
            activity_id=call_id,
            kind="tool",
            name="retrieve_document_materials",
            label="提取文档素材",
            status="completed" if output else "failed",
            node="docs_research",
            output_summary={"mode": "retrieve", "material_count": len(output)},
        )
        return output

    results = await asyncio.gather(*(extract(doc_id) for doc_id in pending_doc_ids), return_exceptions=True)
    for doc_id, result in zip(pending_doc_ids, results, strict=True):
        if isinstance(result, BaseException):
            failures.append(doc_id)
            technical_failures.append(doc_id)
        elif not result:
            failures.append(doc_id)
        else:
            materials.extend(result)
    if not materials and pending_doc_ids and len(technical_failures) == len(pending_doc_ids):
        emit(
            "warning",
            {
                "code": "DOCUMENT_EXTRACTION_DEGRADED",
                "message": "参考文档暂时无法提取，将使用联网素材或通用知识继续。",
            },
        )
    if failures and state["document_coverage_mode"] == "all_required":
        raise AppError(
            422,
            "DOCUMENT_COVERAGE_REQUIRED",
            "At least one required document could not be extracted.",
            details={"failed_doc_ids": failures},
        )
    normalized = normalize_material_library(materials, documents=state.get("document_meta", []))
    await services.artifacts.replace_materials(
        state["artifact_id"],
        response_id=state["response_id"],
        sources=set(pending_doc_ids),
        materials=normalized,
    )
    return {"document_material_status": "degraded" if failures else "completed"}


async def web_material_worker(state: WechatArticleState) -> WechatArticleState:
    if not state.get("need_web_search"):
        return {"web_material_status": "skipped"}
    services = await get_graph_services()
    call_id = new_activity_id("web_material")
    activity(
        activity_id=call_id,
        kind="tool",
        name="ark.web_search",
        label="搜索网络写作素材",
        status="running",
        node="web_material_worker",
        input_summary={"categories": 4},
    )
    try:
        result = await services.llm.web_search(
            run_id=state["run_id"],
            phase="web_material",
            system_prompt=WEB_MATERIAL_SYSTEM_PROMPT,
            input_text=render_input(
                WebMaterialInput(
                    user_request=state["current_user_input"],
                    research_direction=ResearchDirectionInput.model_validate(state["research_direction"]),
                    web_info_overview=state.get("web_info_overview", ""),
                )
            ),
            output_model=WebMaterialOutput,
            max_keyword=4,
            event_callback=_model_event,
        )
    except AppError as exc:
        activity(
            activity_id=call_id,
            kind="tool",
            name="ark.web_search",
            label="网络素材搜索暂不可用",
            status="degraded",
            node="web_material_worker",
            error={"code": exc.code, "message": exc.message},
        )
        public_text("网络素材暂不可用，我将使用文档素材或通用知识继续生成。")
        return {"web_material_status": "degraded"}
    materials = web_search_materials(result.output, annotations=result.annotations, queries=result.queries)
    # Persistence failures are workflow failures. They must not be disguised as a
    # recoverable search degradation after the upstream search already succeeded.
    await services.artifacts.replace_materials(
        state["artifact_id"],
        response_id=state["response_id"],
        kinds={"factual", "resource", "creative_reference", "popular_culture"},
        materials=materials,
    )
    activity(
        activity_id=call_id,
        kind="tool",
        name="ark.web_search",
        label="网络写作素材搜索完成",
        status="completed" if materials else "degraded",
        node="web_material_worker",
        output_summary={"material_count": len(materials), "query_count": len(result.queries)},
    )
    return {"web_material_status": "completed" if materials else "degraded"}


async def material_normalize(state: WechatArticleState) -> WechatArticleState:
    services = await get_graph_services()
    current = await _require_artifact(state)
    materials = normalize_material_library(current.material_library, documents=state.get("document_meta", []))
    await services.artifacts.update_stage(
        state["artifact_id"],
        response_id=state["response_id"],
        current_stage="material_compaction",
        values={"material_library": materials},
    )
    return {"current_stage": "material_compaction"}


async def material_compacter(state: WechatArticleState) -> WechatArticleState:
    services = await get_graph_services()
    current = await _require_artifact(state)
    materials = [replace_reference_chunks_with_summary(item) for item in current.material_library or []]
    budget = safe_material_token_budget(services.settings.ark_context_window)
    if estimated_material_tokens(materials) > budget:

        async def compact(item: dict[str, Any]) -> dict[str, Any]:
            if item.get("material_kind") in {
                "resource",
                "creative_reference",
                "popular_culture",
            }:
                return item
            chunks = [chunk for chunk in item.get("orig_chunks") or [] if chunk.get("content")]
            if item.get("metadata", {}).get("extraction_mode") == "raw" or not chunks:
                return item
            chunk_ids = [str(chunk["chunk_id"]) for chunk in chunks]
            output_model = dynamic_string_output(
                "CompactedChunks",
                chunk_ids,
                description="忠实压缩该素材片段且保留事实限定条件后的内容",
            )
            try:
                result = await services.llm.structured(
                    run_id=state["run_id"],
                    phase="material_compaction",
                    system_prompt=MATERIAL_COMPACTION_SYSTEM_PROMPT,
                    input_text=render_input(
                        MaterialCompactionInput(
                            material_kind=item.get("material_kind", "user_document"),
                            material_summary=str(item.get("summary") or ""),
                            research_direction=ResearchDirectionInput.model_validate(
                                state["research_direction"]
                            ),
                            chunks=[
                                CompactionChunkInput(
                                    chunk_id=str(chunk["chunk_id"]),
                                    path=str(chunk.get("path") or ""),
                                    content=str(chunk.get("content") or ""),
                                )
                                for chunk in chunks
                            ],
                        )
                    ),
                    output_model=output_model,
                    event_callback=_model_event,
                )
            except AppError:
                return item
            values = result.model_dump()
            if set(values) != set(chunk_ids) or any(not str(values[key]).strip() for key in chunk_ids):
                return item
            compacted = [
                (
                    {**chunk, "content": str(values[str(chunk["chunk_id"])]).strip()}
                    if str(chunk.get("chunk_id") or "") in values
                    else chunk
                )
                for chunk in item.get("orig_chunks") or []
            ]
            return {**item, "orig_chunks": compacted}

        materials = list(await asyncio.gather(*(compact(item) for item in materials)))
    materials, warnings = trim_materials_to_budget(
        materials, context_window=services.settings.ark_context_window
    )
    for warning in warnings:
        emit("warning", warning)
    await services.artifacts.update_stage(
        state["artifact_id"],
        response_id=state["response_id"],
        current_stage="material_conflict_check",
        values={"material_library": materials},
    )
    return {"current_stage": "material_conflict_check"}


async def conflict_checker(state: WechatArticleState) -> WechatArticleState:
    services = await get_graph_services()
    current = await _require_artifact(state)
    chunks = conflict_chunks(current.material_library or [])
    if len(chunks) < 2:
        return {"material_conflicts": [], "current_stage": "material_finalize"}
    try:
        result = await services.llm.structured(
            run_id=state["run_id"],
            phase="conflict_checker",
            system_prompt=CONFLICT_CHECK_SYSTEM_PROMPT,
            input_text=render_input(
                ConflictCheckInput(
                    research_direction=ResearchDirectionInput.model_validate(state["research_direction"]),
                    chunks=[ConflictChunkInput.model_validate(chunk) for chunk in chunks],
                )
            ),
            output_model=ConflictCheckOutput,
            event_callback=_model_event,
        )
    except AppError as exc:
        emit(
            "warning",
            {
                "code": "CONFLICT_CHECK_DEGRADED",
                "message": "素材冲突检查暂不可用，已保留全部素材继续生成。",
                "error": exc.code,
            },
        )
        return {"material_conflicts": [], "current_stage": "material_finalize"}
    known_ids = {str(chunk["chunk_id"]) for chunk in chunks}
    conflicts: list[dict[str, Any]] = []
    by_id = {str(chunk["chunk_id"]): chunk for chunk in chunks}
    for group in result.conflicts:
        ids = list(dict.fromkeys(value for value in group.conflict_chunk_ids if value in known_ids))
        if len(ids) >= 2:
            conflicts.append(
                {
                    "conflict_chunk_ids": ids,
                    "user_facing_message": group.user_facing_message,
                    "sources": [
                        {
                            "chunk_id": chunk_id,
                            "path": by_id[chunk_id]["path"],
                            "ref": by_id[chunk_id]["ref"],
                            "excerpt": by_id[chunk_id]["content"][:500],
                        }
                        for chunk_id in ids
                    ],
                }
            )
    if not conflicts:
        if result.conflicts:
            emit(
                "warning",
                {
                    "code": "CONFLICT_CHECK_INVALID_GROUPS",
                    "message": "素材冲突检查返回了无效来源组合，已保留全部素材继续生成。",
                },
            )
        return {"material_conflicts": [], "current_stage": "material_finalize"}
    conflict_artifact = [
        {
            "message": group["user_facing_message"],
            "sources": group["sources"],
        }
        for group in conflicts
    ]
    await services.artifacts.update_stage(
        state["artifact_id"],
        response_id=state["response_id"],
        current_stage="material_conflict_review",
        status="waiting_for_input",
    )
    artifact(
        artifact_id=state["artifact_id"],
        revision=state["revision"],
        stage="material_conflicts",
        status="pending_review",
        content={"conflicts": conflict_artifact},
    )
    return {
        "material_conflicts": conflicts,
        "pending_interrupt_id": prefixed_id("int"),
        "current_stage": "material_conflict_review",
        "status": "needs_clarification",
    }


def route_material_conflicts(state: WechatArticleState) -> str:
    return "review" if state.get("material_conflicts") else "continue"


def conflict_interrupt(state: WechatArticleState) -> Command[Any]:
    resume = interrupt(
        {
            "interrupt_id": state["pending_interrupt_id"],
            "response_id": state["response_id"],
            "stage": "material_conflict_review",
            "artifact_id": state["artifact_id"],
            "revision": state["revision"],
            "form": {
                "form_type": "agent_artifact_review",
                "review_type": "material_conflict",
                "title": "请选择素材冲突的处理方式",
                "description": "系统发现会影响文章结论的素材冲突，请选择判断依据。",
                "selection_mode": "single",
                "options": [
                    {"id": "document_priority", "label": "以我提供的文档为准"},
                    {"id": "automatic_authority", "label": "按来源权威性自动判断"},
                    {"id": "custom_feedback", "label": "按我的补充意见处理"},
                ],
                "custom_option": {"enabled": True, "feedback_label": "补充处理意见"},
                "fields": [
                    {
                        "message": group["user_facing_message"],
                        "sources": group.get("sources", []),
                    }
                    for group in state.get("material_conflicts", [])
                ],
            },
        }
    )
    if not isinstance(resume, dict):
        raise ValueError("conflict review resume must be an object")
    if resume.get("type") == "cancel":
        return Command(update={"status": "cancelled"}, goto="cancelled")
    if resume.get("type") == "supersede":
        return Command(update={"status": "superseded"}, goto="superseded")
    selection = resume.get("selection")
    option_id = str(selection.get("option_id") or "") if isinstance(selection, dict) else ""
    if option_id not in {"document_priority", "automatic_authority", "custom_feedback"}:
        raise ValueError("invalid conflict resolution selection")
    feedback = str(resume.get("feedback") or "").strip()
    if option_id == "custom_feedback" and not feedback:
        raise ValueError("custom conflict feedback is required")
    return Command(
        update={
            "response_id": str(resume["response_id"]),
            "conflict_resolution": option_id,
            "conflict_feedback": feedback,
            "pending_interrupt_id": "",
            "status": "running",
        },
        goto="conflict_handler",
    )


async def conflict_handler(state: WechatArticleState) -> WechatArticleState:
    services = await get_graph_services()
    current = await _require_artifact(state)
    chunks = conflict_chunks(current.material_library or [])
    by_id = {str(chunk["chunk_id"]): chunk for chunk in chunks}
    groups: list[ConflictHandlerGroupInput] = []
    ordered_ids: list[str] = []
    for conflict in state.get("material_conflicts", []):
        ids = [value for value in conflict.get("conflict_chunk_ids", []) if value in by_id]
        ordered_ids.extend(ids)
        groups.append(
            ConflictHandlerGroupInput(
                user_facing_message=str(conflict.get("user_facing_message") or ""),
                chunks=[ConflictChunkInput.model_validate(by_id[value]) for value in ids],
            )
        )
    ordered_ids = list(dict.fromkeys(ordered_ids))
    output_model = dynamic_string_output(
        "ConflictResolutionNotes",
        ordered_ids,
        description="不可信素材的具体冲突提示；可信素材返回空字符串",
    )
    try:
        result = await services.llm.structured(
            run_id=state["run_id"],
            phase="conflict_handler",
            system_prompt=CONFLICT_HANDLER_SYSTEM_PROMPT,
            input_text=render_input(
                ConflictHandlerInput.model_validate(
                    {
                        "research_direction": state["research_direction"],
                        "decision": state.get("conflict_resolution", "automatic_authority"),
                        "feedback": state.get("conflict_feedback", ""),
                        "conflicts": groups,
                    }
                )
            ),
            output_model=output_model,
            event_callback=_model_event,
        )
        materials = apply_conflict_notes(current.material_library or [], result.model_dump())
    except AppError as exc:
        emit(
            "warning",
            {
                "code": "CONFLICT_HANDLER_DEGRADED",
                "message": "素材冲突处理暂不可用，已保留全部原始素材继续生成。",
                "error": exc.code,
            },
        )
        materials = list(current.material_library or [])
    await services.artifacts.update_stage(
        state["artifact_id"],
        response_id=state["response_id"],
        current_stage="material_finalize",
        values={"material_library": materials},
    )
    return {"current_stage": "material_finalize", "material_conflicts": []}


async def finalize_material_research(
    state: WechatArticleState, runtime: Runtime[RuntimeContext]
) -> WechatArticleState:
    services = await get_graph_services()
    current = await _require_artifact(state)
    materials = normalize_material_library(current.material_library, documents=state.get("document_meta", []))
    await services.artifacts.update_stage(
        state["artifact_id"],
        response_id=state["response_id"],
        current_stage="docs_research",
        values={"material_library": materials},
    )
    await services.artifacts.mark_approved(
        state["artifact_id"],
        response_id=state["response_id"],
        stage="docs_research",
        next_stage="task_spec",
    )
    sources = material_source_names(materials)
    artifact(
        artifact_id=state["artifact_id"],
        revision=state["revision"],
        stage="material_sources",
        status="completed",
        content=sources,
        summary={"source_count": len(sources)},
    )
    if _context(runtime).get("debug_enabled"):
        artifact(
            artifact_id=state["artifact_id"],
            revision=state["revision"],
            stage="material_library",
            status=(
                "degraded"
                if state.get("document_material_status") == "degraded"
                or state.get("web_material_status") == "degraded"
                else "completed"
            ),
            content=materials,
            summary={
                "material_count": len(materials),
                "generation_basis": _generation_basis(materials),
            },
        )
    return {"current_stage": "task_spec"}


async def generate_task_spec(state: WechatArticleState) -> WechatArticleState:
    services = await get_graph_services()
    current = await _require_artifact(state)
    generation_basis = _generation_basis(current.material_library)
    is_first_candidate = current.task_spec is None
    result = await services.llm.structured(
        run_id=state["run_id"],
        phase="task_spec",
        system_prompt=TASK_SPEC_SYSTEM_PROMPT,
        input_text=render_input(
            TaskSpecGenerationInput(
                user_request=state["current_user_input"],
                research_direction=(
                    ResearchDirectionInput.model_validate(current.research_direction)
                    if current.research_direction
                    else None
                ),
                memory=state.get("session_memory", {}).get("task_spec", ""),
                coverage_mode=current.document_coverage_mode or "best_effort",
                generation_basis=generation_basis,
                materials=clean_material_summaries(current.material_library),
                web_info_overview=state.get("web_info_overview", ""),
                feedback=state.get("review_feedback", ""),
                current_candidate=clean_task_spec(current.task_spec),
            )
        ),
        output_model=TaskSpecOutput,
        event_callback=_model_event,
    )
    user_facing_message = result.user_facing_message
    if generation_basis == "general_knowledge" and is_first_candidate:
        notice = "未找到可用于本次主题的参考内容，后续将基于通用知识生成文章。"
        user_facing_message = f"{notice}\n\n{user_facing_message}" if user_facing_message else notice
    public_text(user_facing_message)
    await services.artifacts.update_stage(
        state["artifact_id"],
        response_id=state["response_id"],
        current_stage="task_spec_review",
        values={"task_spec": result.artifact.model_dump()},
        status="waiting_for_input",
    )
    artifact(
        artifact_id=state["artifact_id"],
        revision=state["revision"],
        stage="task_spec",
        status="pending_review",
        content=result.artifact.model_dump(),
    )
    return {
        "current_stage": "task_spec_review",
        "pending_interrupt_id": prefixed_id("int"),
        "review_feedback": "",
    }


async def generate_outline(state: WechatArticleState) -> WechatArticleState:
    services = await get_graph_services()
    current = await _require_artifact(state)
    generation_basis = _generation_basis(current.material_library)
    result = await services.llm.structured(
        run_id=state["run_id"],
        phase="outline",
        system_prompt=OUTLINE_SYSTEM_PROMPT,
        input_text=render_input(
            OutlineGenerationInput(
                task_spec=require_task_spec(current.task_spec),
                generation_basis=generation_basis,
                materials=clean_material_summaries(current.material_library),
                web_info_overview=state.get("web_info_overview", ""),
                memory=state.get("session_memory", {}).get("outline", ""),
                feedback=state.get("review_feedback", ""),
                current_candidate=outline_markdown(current.outline),
            )
        ),
        output_model=MarkdownOutput,
        event_callback=_model_event,
    )
    public_text(result.user_facing_message)
    await services.artifacts.update_stage(
        state["artifact_id"],
        response_id=state["response_id"],
        current_stage="outline_review",
        values={"outline": {"markdown": result.markdown}},
        status="waiting_for_input",
    )
    artifact(
        artifact_id=state["artifact_id"],
        revision=state["revision"],
        stage="outline",
        status="pending_review",
        content=result.markdown,
    )
    return {
        "current_stage": "outline_review",
        "pending_interrupt_id": prefixed_id("int"),
        "review_feedback": "",
    }


async def generate_article(state: WechatArticleState) -> WechatArticleState:
    services = await get_graph_services()
    current = await _require_artifact(state)
    generation_basis = _generation_basis(current.material_library)
    result = await services.llm.structured(
        run_id=state["run_id"],
        phase="article",
        system_prompt=ARTICLE_SYSTEM_PROMPT,
        input_text=render_input(
            ArticleGenerationInput(
                task_spec=require_task_spec(current.task_spec),
                outline_markdown=outline_markdown(current.outline),
                generation_basis=generation_basis,
                materials=clean_material_evidence(current.material_library),
                memory=state.get("session_memory", {}).get("article", ""),
                feedback=state.get("review_feedback", ""),
                current_candidate=current.article_markdown or "",
            )
        ),
        output_model=MarkdownOutput,
        event_callback=_model_event,
    )
    public_text(result.user_facing_message)
    await services.artifacts.update_stage(
        state["artifact_id"],
        response_id=state["response_id"],
        current_stage="article_review",
        values={"article_markdown": result.markdown},
        status="waiting_for_input",
    )
    artifact(
        artifact_id=state["artifact_id"],
        revision=state["revision"],
        stage="article_markdown",
        status="pending_review",
        content=result.markdown,
    )
    return {
        "current_stage": "article_review",
        "pending_interrupt_id": prefixed_id("int"),
        "review_feedback": "",
    }


def _review_node(stage: str, title: str, next_node: str) -> Any:
    def review(state: WechatArticleState) -> Command[Any]:
        resume = interrupt(
            {
                "interrupt_id": state["pending_interrupt_id"],
                "response_id": state["response_id"],
                "stage": f"{stage}_review",
                "artifact_id": state["artifact_id"],
                "revision": state["revision"],
                "form": {
                    "form_type": "agent_artifact_review",
                    "title": title,
                    "description": "接受后将进入下一阶段；也可以重新生成或按反馈修改。",
                    "fields": [],
                },
            }
        )
        if not isinstance(resume, dict):
            raise ValueError("review resume must be an object")
        if resume.get("type") == "cancel":
            return Command(update={"status": "cancelled"}, goto="cancelled")
        if resume.get("type") == "supersede":
            return Command(update={"status": "superseded"}, goto="superseded")
        decision = str(resume.get("decision") or "")
        response_id = str(resume.get("response_id") or "")
        feedback = str(resume.get("feedback") or "").strip()
        if decision not in {"approve", "revise", "regenerate"} or not response_id:
            raise ValueError("invalid review decision")
        if decision == "revise" and not feedback:
            raise ValueError("revise feedback is required")
        goto = f"mark_{stage}_approved" if decision == "approve" else f"generate_{stage}"
        return Command(
            update={
                "response_id": response_id,
                "review_action": decision,
                "review_feedback": feedback,
                "pending_interrupt_id": "",
                "status": "running",
            },
            goto=goto,
        )

    review.__name__ = f"review_{stage}"
    return review


def _mark_approved_node(stage: str, next_stage: str) -> Any:
    async def mark(state: WechatArticleState) -> WechatArticleState:
        services = await get_graph_services()
        await services.artifacts.mark_approved(
            state["artifact_id"],
            response_id=state["response_id"],
            stage=stage,  # type: ignore[arg-type]
            next_stage=next_stage,
        )
        return {"current_stage": next_stage, "review_feedback": ""}

    mark.__name__ = f"mark_{stage}_approved"
    return mark


async def generate_images(state: WechatArticleState) -> WechatArticleState:
    services = await get_graph_services()
    current = await _require_artifact(state)
    markdown = current.article_markdown or ""
    positions = _heading_positions(markdown)
    plan = await services.llm.structured(
        run_id=state["run_id"],
        phase="ai_image_plan",
        system_prompt=IMAGE_PLAN_SYSTEM_PROMPT,
        input_text=render_input(
            ImagePlanInput(
                task_spec=require_task_spec(current.task_spec),
                positions=[ImagePositionInput.model_validate(position) for position in positions],
                max_count=services.settings.image_max_count,
            )
        ),
        output_model=ImagePlanOutput,
        event_callback=_model_event,
    )
    public_text(plan.user_facing_message)
    allowed = {item["position_id"] for item in positions}
    candidates = [item for item in plan.images if item.position_id in allowed][
        : services.settings.image_max_count
    ]

    async def generate(item: Any) -> dict[str, Any] | None:
        position = next(value for value in positions if value["position_id"] == item.position_id)
        call_id = new_activity_id("generate_image")
        activity(
            activity_id=call_id,
            kind="tool",
            name="generate_image",
            label=f"生成配图：{item.caption}",
            status="running",
            node="ai_image",
        )
        try:
            url = await services.seedream.generate(_grounded_image_prompt(item.prompt, position))
        except AppError as exc:
            activity(
                activity_id=call_id,
                kind="tool",
                name="generate_image",
                label=f"生成配图：{item.caption}",
                status="failed",
                node="ai_image",
                error={"code": exc.code, "message": exc.message},
            )
            return None
        activity(
            activity_id=call_id,
            kind="tool",
            name="generate_image",
            label=f"生成配图：{item.caption}",
            status="completed",
            node="ai_image",
        )
        insertion_position = {
            "position_id": position["position_id"],
            "heading_path": position["heading_path"],
            "paragraph_ordinal": position["paragraph_ordinal"],
        }
        return {"url": url, "caption": item.caption, "insertion_position": insertion_position}

    generated = await asyncio.gather(*(generate(item) for item in candidates))
    images = [item for item in generated if item is not None]
    if candidates and not images:
        emit(
            "warning",
            {"code": "ALL_IMAGES_FAILED", "message": "全部配图失败，将交付无图 HTML。"},
        )
    await services.artifacts.update_stage(
        state["artifact_id"],
        response_id=state["response_id"],
        current_stage="html_layout",
        values={"images": images},
    )
    artifact(
        artifact_id=state["artifact_id"],
        revision=state["revision"],
        stage="images",
        status="completed" if images or not candidates else "degraded",
        content=images,
    )
    return {"current_stage": "html_layout"}


def _layout_public_summary(event: LayoutEvent) -> dict[str, Any]:
    if event.stage == "select_theme":
        return {
            key: event.details[key] for key in ("theme_id", "reason") if event.details.get(key) is not None
        }
    if event.stage == "render_markdown":
        inserted = event.details.get("inserted_images")
        return {
            "theme_id": event.details.get("theme_id"),
            "image_count": len(inserted) if isinstance(inserted, list) else None,
        }
    if event.stage == "validate_html":
        return {
            "theme_id": event.details.get("theme_id"),
            "valid": event.details.get("valid"),
            "error_count": len(event.details.get("errors") or []),
            "warning_count": len(event.details.get("warnings") or []),
        }
    return {
        key: event.details[key] for key in ("from", "to", "renderer") if event.details.get(key) is not None
    }


async def render_html(state: WechatArticleState) -> WechatArticleState:
    services = await get_graph_services()
    current = await _require_artifact(state)

    skill_activity_id = new_activity_id("wechat_layout")
    activity(
        activity_id=skill_activity_id,
        kind="skill",
        name="wechat_layout",
        label="应用微信公众号排版能力",
        status="running",
        node="render_html",
        input_summary={"renderer": services.settings.html_layout_renderer},
    )

    layout_activity_ids: dict[tuple[str, int], str] = {}

    def on_layout_event(event: LayoutEvent) -> None:
        activity_id = layout_activity_ids.setdefault(
            (event.stage, event.attempt),
            new_activity_id(f"layout_{event.stage}"),
        )
        kind: Literal["skill", "tool"] = "skill" if event.stage in {"select_theme", "fallback"} else "tool"
        label = {
            "select_theme": "选择排版主题",
            "render_markdown": "转换 Markdown 为 HTML",
            "validate_html": "校验 HTML 结构",
            "fallback": "执行排版降级",
        }[event.stage]
        public_summary = _layout_public_summary(event)
        activity(
            activity_id=activity_id,
            kind=kind,
            name=f"wechat_layout.{event.stage}",
            label=label,
            status=event.status,
            node="render_html",
            input_summary={"attempt": event.attempt} if event.status == "running" else None,
            output_summary=public_summary if event.status in {"completed", "degraded"} else None,
            error={"code": "LAYOUT_STEP_FAILED", "message": label} if event.status == "failed" else None,
        )
        record(
            "tool_calls",
            {
                "id": activity_id,
                "name": f"wechat_layout.{event.stage}",
                "kind": kind,
                "node": "render_html",
                "status": event.status,
                "attempt": event.attempt,
                "arguments": event.details if event.status == "running" else None,
                "result": event.details if event.status in {"completed", "degraded"} else None,
                "error": event.details if event.status == "failed" else None,
            },
        )

    public_text("我将根据文章内容应用适合公众号阅读的排版主题，并检查 HTML 结构。")
    requested_theme_id: str | None = None
    selected_mode = services.settings.html_layout_renderer
    selection_degraded = False
    result = None

    if selected_mode == "skill_driven":
        skill_mode_activity_id = new_activity_id("layout_skill_driven")
        activity(
            activity_id=skill_mode_activity_id,
            kind="skill",
            name="wechat_layout.skill_driven",
            label="使用排版 Agent 与 Skill",
            status="running",
            node="render_html",
            input_summary={"renderer": "skill_driven"},
        )
        engine_activity_ids: dict[str, str] = {}
        engine_activity_started: dict[str, float] = {}

        async def on_engine_event(kind: str, data: dict[str, Any]) -> None:
            if kind in {"reasoning_delta", "usage"}:
                await _model_event(kind, data)
                return
            if kind == "agent_output_text":
                public_text(str(data.get("text") or ""))
                return
            if kind == "skill_disclosure":
                for skill_id in data.get("skills") or []:
                    disclosure_id = new_activity_id(f"engine_skill_{skill_id}")
                    activity(
                        activity_id=disclosure_id,
                        kind="skill",
                        name=f"agent_engine.skill.{skill_id}",
                        label=f"加载 Skill：{skill_id}",
                        status="completed",
                        node="render_html",
                        output_summary={"level": "L0"},
                    )
                    record(
                        "tool_calls",
                        {
                            "id": disclosure_id,
                            "kind": "skill",
                            "name": skill_id,
                            "node": "render_html",
                            "status": "completed",
                            "result": data if current_debug_trace() is not None else None,
                        },
                    )
                record(
                    "tool_calls",
                    {
                        "id": f"{skill_mode_activity_id}_disclosure",
                        "kind": "skill",
                        "name": "agent_engine.skill_disclosure",
                        "node": "render_html",
                        "status": "completed",
                        "result": data,
                    },
                )
                return
            if kind.startswith("tool_") and kind not in {"tool_duplicate_ignored"}:
                call_id = str(data.get("call_id") or data.get("tool") or "unknown")
                activity_id = engine_activity_ids.setdefault(
                    call_id,
                    new_activity_id(f"engine_{data.get('tool') or 'tool'}"),
                )
                tool_name = str(data.get("tool") or "unknown")
                activity_kind: Literal["tool", "skill"] = (
                    "skill" if tool_name == "load_rendering_skill_asset" else "tool"
                )
                label = _AGENT_LAYOUT_TOOL_LABELS.get(tool_name, tool_name)
                status = cast(
                    Literal["running", "completed", "failed"] | None,
                    {
                        "tool_started": "running",
                        "tool_completed": "completed",
                        "tool_failed": "failed",
                    }.get(kind),
                )
                if status is None:
                    return
                now = monotonic()
                if status == "running":
                    engine_activity_started[call_id] = now
                elapsed_ms = None
                if status != "running" and call_id in engine_activity_started:
                    elapsed_ms = round((now - engine_activity_started[call_id]) * 1000, 3)
                activity(
                    activity_id=activity_id,
                    kind=activity_kind,
                    name=f"agent_engine.{tool_name}",
                    label=label,
                    status=status,
                    node="render_html",
                    output_summary=(
                        {"completed": True, "attempts": data.get("attempts")}
                        if status == "completed"
                        else None
                    ),
                    error=(
                        {
                            "code": str(data.get("error") or "AGENT_TOOL_FAILED"),
                            "message": f"{label}失败，Agent 将在预算内调整或降级。",
                        }
                        if status == "failed"
                        else None
                    ),
                )
                record(
                    "tool_calls",
                    {
                        "id": activity_id,
                        "call_id": call_id,
                        "kind": activity_kind,
                        "name": tool_name,
                        "node": "render_html",
                        "status": status,
                        "arguments": data.get("arguments"),
                        "result": data.get("result"),
                        "error": (
                            {
                                "code": data.get("error"),
                                "class": data.get("error_class"),
                                "message": data.get("message"),
                            }
                            if status == "failed"
                            else None
                        ),
                        "attempts": data.get("attempts"),
                        "elapsed_ms": elapsed_ms,
                    },
                )
                return
            if kind in {
                "engine_started",
                "engine_completed",
                "engine_degraded",
                "engine_failed",
                "engine_cancelled",
                "context_preflight",
                "context_compacted",
                "final_output_invalid",
                "tool_duplicate_ignored",
            }:
                record(
                    "tool_calls",
                    {
                        "id": f"{skill_mode_activity_id}_{kind}",
                        "kind": "skill",
                        "name": f"agent_engine.{kind}",
                        "node": "render_html",
                        "status": "completed"
                        if kind not in {"engine_degraded", "engine_failed"}
                        else "degraded",
                        "result": data,
                    },
                )

        engine_budget = AgentBudget(
            max_steps=services.settings.agent_engine_max_steps,
            max_tool_calls=services.settings.agent_engine_max_tool_calls,
            max_context_tokens=services.settings.agent_engine_max_context_tokens,
            context_reserve_tokens=services.settings.agent_engine_context_reserve_tokens,
            recent_full_rounds=services.settings.agent_engine_recent_full_rounds,
            max_compactions=services.settings.agent_engine_max_compactions,
            call_timeout_seconds=services.settings.agent_engine_call_timeout_seconds,
            total_timeout_seconds=services.settings.agent_engine_total_timeout_seconds,
            max_same_tool_failures=services.settings.agent_engine_max_same_tool_failures,
            max_tool_result_chars=services.settings.agent_engine_max_tool_result_chars,
        )
        fallback_reason: str | None = None
        try:
            agent_result, rendered = await run_layout_agent(
                gateway=services.llm,
                run_id=state["run_id"],
                markdown=current.article_markdown or "",
                images=[item.model_dump() for item in current.images or []],
                task_spec=current.task_spec or {},
                default_theme=services.settings.html_layout_default_theme,
                budget=engine_budget,
                event_callback=on_engine_event,
                debug=current_debug_trace() is not None,
            )
        except Exception as exc:
            agent_result = None
            rendered = None
            fallback_reason = type(exc).__name__
        else:
            assert agent_result is not None
            fallback_reason = agent_result.fallback_reason
        if rendered is not None and agent_result is not None and agent_result.status == "completed":
            result = rendered
            if agent_result.user_facing_message:
                public_text(agent_result.user_facing_message)
            engine_degraded = bool(result.fallback_used)
            activity(
                activity_id=skill_mode_activity_id,
                kind="skill",
                name="wechat_layout.skill_driven",
                label="使用排版 Agent 与 Skill",
                status="degraded" if engine_degraded else "completed",
                node="render_html",
                output_summary={
                    "theme_id": result.theme_id,
                    "steps": agent_result.steps,
                    "tool_calls": agent_result.tool_calls,
                    "compressed": agent_result.compressed,
                },
            )
            selection_degraded = engine_degraded
        else:
            selection_degraded = True
            selected_mode = "llm_decide"
            activity(
                activity_id=skill_mode_activity_id,
                kind="skill",
                name="wechat_layout.skill_driven",
                label="使用排版 Agent 与 Skill",
                status="degraded",
                node="render_html",
                output_summary={
                    "fallback_renderer": "llm_decide",
                    "reason": fallback_reason or "AGENT_LAYOUT_INCOMPLETE",
                },
            )
            record(
                "fallbacks",
                {
                    "source": "skill_driven",
                    "target": "llm_decide",
                    "reason": fallback_reason or "AGENT_LAYOUT_INCOMPLETE",
                },
            )
            public_text("排版 Agent 未能在本轮预算内完成校验，已切换到严格主题选择继续排版。")

    if result is None and selected_mode == "deterministic":
        requested_theme_id = services.settings.html_layout_default_theme
    elif result is None and selected_mode == "llm_decide":
        catalog_activity_id = new_activity_id("layout_theme_catalog")
        selection_activity_id = new_activity_id("layout_theme_selection")
        activity(
            activity_id=catalog_activity_id,
            kind="tool",
            name="wechat_layout.describe_supported_themes",
            label="读取可用排版主题目录",
            status="running",
            node="render_html",
            input_summary={"registry": "bundled_allowlist"},
        )
        try:
            registry = default_registry()
            catalog = describe_supported_themes(registry)
            activity(
                activity_id=catalog_activity_id,
                kind="tool",
                name="wechat_layout.describe_supported_themes",
                label="读取可用排版主题目录",
                status="completed",
                node="render_html",
                output_summary={"theme_count": len(registry.ids())},
            )
            record(
                "tool_calls",
                {
                    "id": catalog_activity_id,
                    "name": "wechat_layout.describe_supported_themes",
                    "kind": "tool",
                    "node": "render_html",
                    "status": "completed",
                    "arguments": {"registry": "bundled_allowlist"},
                    "result": {"theme_count": len(registry.ids()), "catalog": catalog},
                },
            )
        except Exception as exc:
            selection_degraded = True
            requested_theme_id = services.settings.html_layout_default_theme
            activity(
                activity_id=catalog_activity_id,
                kind="tool",
                name="wechat_layout.describe_supported_themes",
                label="读取可用排版主题目录",
                status="failed",
                node="render_html",
                error={"code": type(exc).__name__, "message": str(exc)},
            )
            record(
                "tool_calls",
                {
                    "id": catalog_activity_id,
                    "name": "wechat_layout.describe_supported_themes",
                    "kind": "tool",
                    "node": "render_html",
                    "status": "failed",
                    "error": {"code": type(exc).__name__, "message": str(exc)},
                },
            )
            public_text("主题目录暂不可用，已使用默认排版主题继续生成。")
        else:
            activity(
                activity_id=selection_activity_id,
                kind="skill",
                name="wechat_layout.select_theme_with_llm",
                label="根据任务书选择排版主题",
                status="running",
                node="render_html",
                input_summary={"theme_count": len(registry.ids())},
            )
            try:
                selected = await select_theme_with_llm(
                    llm=services.llm,
                    run_id=state["run_id"],
                    task_spec=dict(current.task_spec or {}),
                    event_callback=_model_event,
                    registry=registry,
                    theme_catalog=catalog,
                )
            except Exception as exc:
                selection_degraded = True
                requested_theme_id = services.settings.html_layout_default_theme
                activity(
                    activity_id=selection_activity_id,
                    kind="skill",
                    name="wechat_layout.select_theme_with_llm",
                    label="根据任务书选择排版主题",
                    status="failed",
                    node="render_html",
                    error={"code": type(exc).__name__, "message": str(exc)},
                )
                record(
                    "tool_calls",
                    {
                        "id": selection_activity_id,
                        "name": "wechat_layout.select_theme_with_llm",
                        "kind": "skill",
                        "node": "render_html",
                        "status": "degraded",
                        "error": {"code": type(exc).__name__, "message": str(exc)},
                        "fallback": "deterministic",
                    },
                )
                public_text("主题选择服务暂不可用，已使用默认排版主题继续生成。")
            else:
                requested_theme_id = str(selected[0].theme_id)
                public_text(str(selected[0].user_facing_message))
                activity(
                    activity_id=selection_activity_id,
                    kind="skill",
                    name="wechat_layout.select_theme_with_llm",
                    label="根据任务书选择排版主题",
                    status="completed",
                    node="render_html",
                    output_summary={"theme_id": requested_theme_id},
                )
                record(
                    "tool_calls",
                    {
                        "id": selection_activity_id,
                        "name": "wechat_layout.select_theme_with_llm",
                        "kind": "skill",
                        "node": "render_html",
                        "status": "completed",
                        "arguments": {"task_spec": dict(current.task_spec or {})},
                        "result": {
                            "theme_id": requested_theme_id,
                            "user_facing_message": str(selected[0].user_facing_message),
                        },
                    },
                )

    try:
        if result is None:
            renderer = LayoutEngine(default_theme=services.settings.html_layout_default_theme)
            result = renderer.render(
                markdown=current.article_markdown or "",
                images=[item.model_dump() for item in current.images or []],
                task_spec=current.task_spec or {},
                requested_theme_id=requested_theme_id,
                event_callback=on_layout_event,
            )
    except LayoutError as exc:
        activity(
            activity_id=skill_activity_id,
            kind="skill",
            name="wechat_layout",
            label="应用微信公众号排版能力",
            status="failed",
            node="render_html",
            error={"code": type(exc).__name__, "message": str(exc)},
        )
        raise AppError(
            500,
            "HTML_LAYOUT_FAILED",
            "文章内容已生成，但所有 HTML 排版方案均失败。",
            retryable=True,
            stage="html_layout",
            details={"cause": str(exc)},
        ) from exc

    html = result.final_html
    degraded = result.fallback_used or selection_degraded
    record(
        "tool_calls",
        {
            "id": skill_activity_id,
            "name": "wechat_layout",
            "node": "render_html",
            "status": "degraded" if degraded else "completed",
            "renderer": result.renderer_version,
            "theme_id": result.theme_id,
            "selection": result.selection.model_dump(),
            "validation": result.validation_report.model_dump(),
            "fallback_used": degraded,
            "fallback_reason": result.fallback_reason,
            "timings_ms": result.timings_ms,
        },
    )
    activity(
        activity_id=skill_activity_id,
        kind="skill",
        name="wechat_layout",
        label="应用微信公众号排版能力",
        status="degraded" if degraded else "completed",
        node="render_html",
        output_summary={
            "theme_id": result.theme_id,
            "renderer_version": result.renderer_version,
            "warning_count": len(result.validation_report.warnings),
            "fallback_used": degraded,
        },
    )
    await services.artifacts.update_stage(
        state["artifact_id"],
        response_id=state["response_id"],
        current_stage="completed",
        values={"final_html": html},
        status="completed",
    )
    artifact(
        artifact_id=state["artifact_id"],
        revision=state["revision"],
        stage="final_html",
        status="degraded" if degraded else "completed",
        content=html,
    )
    public_text("文章已经生成并完成微信公众号排版。")
    return {"current_stage": "completed", "status": "completed"}


_AGENT_LAYOUT_TOOL_LABELS = {
    "load_rendering_skill_asset": "读取排版 Skill 资产",
    "analyze_markdown_layout": "分析文章结构",
    "get_skill_theme_catalog": "读取 GZH 主题目录",
    "get_skill_component_catalog": "读取主题组件目录",
    "validate_skill_layout_plan": "校验富组件排版计划",
    "assemble_skill_html": "装配主题组件 HTML",
    "validate_skill_html": "校验正文与图片一致性",
}


async def cancelled(state: WechatArticleState) -> WechatArticleState:
    services = await get_graph_services()
    current = await services.artifacts.get(state["artifact_id"])
    if current and current.status == "cancelling":
        await services.artifacts.finish_cancel(state["artifact_id"], response_id=state["response_id"])
    return {"status": "cancelled", "current_stage": "cancelled"}


async def superseded(state: WechatArticleState) -> WechatArticleState:
    services = await get_graph_services()
    await services.artifacts.mark_terminal(
        state["artifact_id"],
        response_id=state["response_id"],
        status="superseded",
        current_stage="superseded",
        last_error={"code": "SUPERSEDED", "retryable": False},
    )
    return {"status": "superseded", "current_stage": "superseded"}


async def _require_artifact(state: WechatArticleState) -> Any:
    services = await get_graph_services()
    current = await services.artifacts.get(state["artifact_id"])
    if current is None:
        raise AppError(404, "ARTIFACT_NOT_FOUND", "Artifact does not exist.")
    return current


def _heading_positions(markdown: str) -> list[dict[str, Any]]:
    positions: list[dict[str, Any]] = []
    path: list[str] = []
    paragraph = 0
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            title = stripped[level:].strip()
            if title:
                path = path[: max(0, level - 1)] + [title]
                paragraph = 0
        elif stripped:
            paragraph += 1
            if path:
                positions.append(
                    {
                        "position_id": f"pos_{len(positions) + 1:03d}",
                        "heading_path": list(path),
                        "paragraph_ordinal": paragraph,
                        "context_text": stripped,
                    }
                )
    return positions


def _grounded_image_prompt(planned_prompt: str, position: dict[str, Any]) -> str:
    heading = " > ".join(str(item) for item in position.get("heading_path") or [])
    context = str(position.get("context_text") or "").strip()
    return (
        f"{planned_prompt.strip()}\n\n"
        "# 相邻正文事实约束\n"
        f"章节：{heading}\n"
        f"正文原文：<article_context>{context}</article_context>\n"
        "上述原文只用于校准图片语义和事实边界。不得新增、替换或改写其中的数字、比例、金额、日期、"
        "条款、机构和流程顺序；不得根据常见模板自行补全事实。若无法可靠呈现这些精确信息，应采用与"
        "正文主体直接相关但不含文字、数字、图表、流程节点或界面标签的场景化画面。"
    )


def _traced_node(name: str, function: Any, *, runtime_arg: bool = False) -> Any:
    async def traced(
        state: WechatArticleState,
        runtime: Runtime[RuntimeContext],
    ) -> Any:
        services = await get_graph_services()
        context = _context(runtime)
        collector = get_or_create(
            enabled=bool(context.get("debug_enabled")),
            run_id=state.get("run_id", ""),
            response_id=str(context.get("response_id") or state.get("response_id", "")),
            request={
                "conversation": context.get("conversation", []),
                "state": state,
            },
            max_chars=services.settings.debug_trace_value_max_chars,
            max_bytes=services.settings.debug_trace_max_bytes,
        )
        call_id = f"node_{name}"
        with use_collector(collector):
            record("nodes", {"id": call_id, "name": name, "status": "running", "input": state})
            activity(
                activity_id=call_id,
                kind="node",
                name=name,
                label=_NODE_LABELS.get(name, name),
                status="running",
                node=name,
            )
            try:
                result = function(state, runtime) if runtime_arg else function(state)
                if inspect.isawaitable(result):
                    result = await result
            except GraphInterrupt:
                record(
                    "nodes",
                    {"id": call_id, "name": name, "status": "waiting_for_input"},
                )
                activity(
                    activity_id=call_id,
                    kind="node",
                    name=name,
                    label=_NODE_LABELS.get(name, name),
                    status="completed",
                    node=name,
                    output_summary={"waiting_for_input": True},
                )
                emit_snapshot(collector, release=True)
                raise
            except Exception as exc:
                record(
                    "nodes",
                    {
                        "id": call_id,
                        "name": name,
                        "status": "failed",
                        "error": {"type": type(exc).__name__, "message": str(exc)},
                    },
                )
                record(
                    "errors",
                    {"source": "node", "node": name, "type": type(exc).__name__, "message": str(exc)},
                )
                activity(
                    activity_id=call_id,
                    kind="node",
                    name=name,
                    label=_NODE_LABELS.get(name, name),
                    status="failed",
                    node=name,
                    error={"code": type(exc).__name__, "message": str(exc)},
                )
                if isinstance(exc, AppError) and state.get("artifact_id") and state.get("response_id"):
                    try:
                        await services.artifacts.mark_terminal(
                            state["artifact_id"],
                            response_id=state["response_id"],
                            status="failed",
                            current_stage=state.get("current_stage", name),
                            last_error={
                                "code": exc.code,
                                "message": exc.message,
                                "retryable": exc.retryable,
                                "stage": exc.stage or name,
                                "details": exc.details,
                            }
                            | {
                                "recovery_action": error_with_recovery(
                                    {"code": exc.code, "retryable": exc.retryable}
                                )["recovery_action"]
                            },
                        )
                    except AppError:
                        pass
                emit_snapshot(collector, release=True)
                if isinstance(exc, AppError):
                    raise RuntimeError(f"{exc.code}: {exc.message}") from exc
                raise
            record(
                "nodes",
                {"id": call_id, "name": name, "status": "completed", "output": result},
            )
            activity(
                activity_id=call_id,
                kind="node",
                name=name,
                label=_NODE_LABELS.get(name, name),
                status="completed",
                node=name,
            )
            if name in {"non_wechat_response", "render_html", "cancelled", "superseded"}:
                emit_snapshot(collector, release=True)
            return result

    traced.__name__ = name
    return traced


_NODE_LABELS = {
    "intent_router": "识别请求类型",
    "intent_clarification_interrupt": "等待确认公众号文章方向",
    "start_planning": "准备文章规划",
    "session_memory": "整理历史审批偏好",
    "orchestrator": "确定本轮生成入口",
    "entry_dispatch": "进入目标阶段",
    "load_document_meta": "读取参考文档信息",
    "material_sufficiency_judge": "判断是否需要联网补充素材",
    "web_info_overview": "了解主题的网络背景",
    "skip_web_info_overview": "跳过网络背景搜索",
    "plan_research_direction": "规划素材搜集方向",
    "clarification_interrupt": "等待确认素材搜集方向",
    "validate_custom_research_direction": "校验自定义素材方向",
    "persist_research_direction": "保存素材搜集方向",
    "expired_material_filter": "筛选仍然有效的历史素材",
    "document_material_worker": "研读文档并提取素材",
    "web_material_worker": "联网搜索写作素材",
    "material_normalize": "整理合并素材",
    "material_compacter": "检查并压缩素材上下文",
    "conflict_checker": "检查素材事实冲突",
    "conflict_interrupt": "等待处理素材冲突",
    "conflict_handler": "按用户选择处理素材冲突",
    "finalize_material_research": "完成素材调研",
    "generate_task_spec": "生成文章任务书",
    "review_task_spec": "等待审核文章任务书",
    "mark_task_spec_approved": "确认文章任务书",
    "generate_outline": "生成文章大纲",
    "review_outline": "等待审核文章大纲",
    "mark_outline_approved": "确认文章大纲",
    "generate_article": "生成未排版文章",
    "review_article": "等待审核未排版文章",
    "mark_article_approved": "确认未排版文章",
    "generate_images": "生成文章配图",
    "render_html": "排版并生成 HTML",
    "cancelled": "取消工作流",
    "superseded": "结束旧需求",
    "non_wechat_response": "结束非文章请求",
}


builder = StateGraph(WechatArticleState, context_schema=RuntimeContext)
builder.add_node("intent_router", _traced_node("intent_router", intent_router, runtime_arg=True))
builder.add_node(
    "intent_clarification_interrupt",
    _traced_node("intent_clarification_interrupt", intent_clarification_interrupt),
)
builder.add_node("non_wechat_response", _traced_node("non_wechat_response", non_wechat_response))
builder.add_node("start_planning", _traced_node("start_planning", start_planning))
builder.add_node("session_memory", _traced_node("session_memory", session_memory, runtime_arg=True))
builder.add_node("orchestrator", _traced_node("orchestrator", orchestrator, runtime_arg=True))
builder.add_node("entry_dispatch", _traced_node("entry_dispatch", entry_dispatch))
builder.add_node("load_document_meta", _traced_node("load_document_meta", load_document_meta))
builder.add_node(
    "material_sufficiency_judge",
    _traced_node("material_sufficiency_judge", material_sufficiency_judge),
)
builder.add_node("web_info_overview", _traced_node("web_info_overview", web_info_overview))
builder.add_node("skip_web_info_overview", _traced_node("skip_web_info_overview", skip_web_info_overview))
builder.add_node("plan_research_direction", _traced_node("plan_research_direction", plan_research_direction))
builder.add_node("clarification_interrupt", _traced_node("clarification_interrupt", clarification_interrupt))
builder.add_node(
    "validate_custom_research_direction",
    _traced_node("validate_custom_research_direction", validate_custom_research_direction),
)
builder.add_node(
    "persist_research_direction",
    _traced_node("persist_research_direction", persist_research_direction),
)
builder.add_node("expired_material_filter", _traced_node("expired_material_filter", expired_material_filter))
builder.add_node(
    "document_material_worker",
    _traced_node("document_material_worker", document_material_worker),
)
builder.add_node("web_material_worker", _traced_node("web_material_worker", web_material_worker))
builder.add_node("material_normalize", _traced_node("material_normalize", material_normalize))
builder.add_node("material_compacter", _traced_node("material_compacter", material_compacter))
builder.add_node("conflict_checker", _traced_node("conflict_checker", conflict_checker))
builder.add_node("conflict_interrupt", _traced_node("conflict_interrupt", conflict_interrupt))
builder.add_node("conflict_handler", _traced_node("conflict_handler", conflict_handler))
builder.add_node(
    "finalize_material_research",
    _traced_node("finalize_material_research", finalize_material_research, runtime_arg=True),
)
builder.add_node("generate_task_spec", _traced_node("generate_task_spec", generate_task_spec))
builder.add_node(
    "review_task_spec",
    _traced_node("review_task_spec", _review_node("task_spec", "请审核文章任务书", "outline")),
)
builder.add_node(
    "mark_task_spec_approved",
    _traced_node("mark_task_spec_approved", _mark_approved_node("task_spec", "outline")),
)
builder.add_node("generate_outline", _traced_node("generate_outline", generate_outline))
builder.add_node(
    "review_outline",
    _traced_node("review_outline", _review_node("outline", "请审核文章大纲", "article")),
)
builder.add_node(
    "mark_outline_approved",
    _traced_node("mark_outline_approved", _mark_approved_node("outline", "article")),
)
builder.add_node("generate_article", _traced_node("generate_article", generate_article))
builder.add_node(
    "review_article",
    _traced_node("review_article", _review_node("article", "请审核未排版文章", "ai_image")),
)
builder.add_node(
    "mark_article_approved",
    _traced_node("mark_article_approved", _mark_approved_node("article", "ai_image")),
)
builder.add_node("generate_images", _traced_node("generate_images", generate_images))
builder.add_node("render_html", _traced_node("render_html", render_html))
builder.add_node("cancelled", _traced_node("cancelled", cancelled))
builder.add_node("superseded", _traced_node("superseded", superseded))

builder.add_edge(START, "intent_router")
builder.add_conditional_edges(
    "intent_router",
    route_intent,
    {
        "wechat": "start_planning",
        "clarify": "intent_clarification_interrupt",
        "other": "non_wechat_response",
    },
)
builder.add_edge("non_wechat_response", END)
builder.add_edge("start_planning", "session_memory")
builder.add_edge("start_planning", "orchestrator")
builder.add_edge(["session_memory", "orchestrator"], "entry_dispatch")
builder.add_conditional_edges(
    "entry_dispatch",
    route_entry,
    {
        "docs_research": "load_document_meta",
        "task_spec": "generate_task_spec",
        "outline": "generate_outline",
        "article": "generate_article",
    },
)
builder.add_edge("load_document_meta", "material_sufficiency_judge")
builder.add_conditional_edges(
    "material_sufficiency_judge",
    route_web_info_overview,
    {"search": "web_info_overview", "skip": "skip_web_info_overview"},
)
builder.add_edge("web_info_overview", "plan_research_direction")
builder.add_edge("skip_web_info_overview", "plan_research_direction")
builder.add_edge("plan_research_direction", "clarification_interrupt")
builder.add_conditional_edges(
    "validate_custom_research_direction",
    route_custom_direction_validation,
    {"ready": "persist_research_direction", "clarify": "clarification_interrupt"},
)
builder.add_edge("persist_research_direction", "expired_material_filter")
builder.add_edge("expired_material_filter", "document_material_worker")
builder.add_edge("expired_material_filter", "web_material_worker")
builder.add_edge(["document_material_worker", "web_material_worker"], "material_normalize")
builder.add_edge("material_normalize", "material_compacter")
builder.add_edge("material_compacter", "conflict_checker")
builder.add_conditional_edges(
    "conflict_checker",
    route_material_conflicts,
    {"review": "conflict_interrupt", "continue": "finalize_material_research"},
)
builder.add_edge("conflict_handler", "finalize_material_research")
builder.add_edge("finalize_material_research", "generate_task_spec")
builder.add_edge("generate_task_spec", "review_task_spec")
builder.add_edge("mark_task_spec_approved", "generate_outline")
builder.add_edge("generate_outline", "review_outline")
builder.add_edge("mark_outline_approved", "generate_article")
builder.add_edge("generate_article", "review_article")
builder.add_edge("mark_article_approved", "generate_images")
builder.add_edge("generate_images", "render_html")
builder.add_edge("render_html", END)
builder.add_edge("cancelled", END)
builder.add_edge("superseded", END)

graph = builder.compile(name="wechat_article")
