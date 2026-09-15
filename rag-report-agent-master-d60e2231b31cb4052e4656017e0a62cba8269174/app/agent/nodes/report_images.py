import asyncio
import json
from typing import Any

import httpx
from pydantic import BaseModel, Field

from app.agent.nodes.common import format_chunks, format_outline_for_report, parse_json_object
from app.config import settings
from app.llm.registry import get_provider
from app.rag.types import ProvidedChunkInput


class ReportImagePlan(BaseModel):
    section_title: str
    image_query: str
    reason: str = ""


class ReportImageCandidate(BaseModel):
    image_id: str
    title: str = ""
    url: str
    description: str = ""
    source: str = ""
    license: str = ""
    section_title: str = ""
    reason: str = ""


class ReportImageInsertionPlan(BaseModel):
    image_id: str
    section_title: str
    insert_after_topic: str
    caption: str
    reason: str = ""


IMAGE_PLAN_SYSTEM = """你是报告配图规划助手。请根据报告大纲和参考资料，判断哪些章节适合插入真实图片。
要求：
1. 只选择确实需要图片辅助理解的章节，不要为了配图而配图。
2. 每个章节最多给出 1 个图片搜索词。
3. 搜索词要适合图片检索，包含核心实体、场景、主题词。
4. 如果没有适合插图的章节，返回空数组。
5. 只输出 JSON，不要解释。
输出格式：
{"plans":[{"section_title":"章节标题","image_query":"图片搜索词","reason":"为什么适合插图"}]}
"""


IMAGE_INSERTION_PLAN_SYSTEM = """你是报告插图位置规划助手。请根据报告大纲、参考资料和可用图片候选，规划图片应该跟随哪个章节和哪个论述主题出现。
要求：
1. 只能使用可用图片候选中的 image_id，不要编造 image_id。
2. 每张图片最多使用一次，每个章节最多使用一张图片。
3. insert_after_topic 不是段落编号，而是写作时应先出现的论述主题，例如“数字经济规模持续增长相关段落之后”。
4. 图片必须和章节主题、候选图片标题/描述高度相关；不相关就不要使用。
5. caption 要适合直接作为 Markdown 图片说明，不超过 30 个中文字符。
6. 只输出 JSON，不要解释。
输出格式：
{"insertions":[{"image_id":"img_1","section_title":"章节标题","insert_after_topic":"应插在什么主题段落之后","caption":"图X：图片说明","reason":"为什么放这里"}]}
"""


def _coerce_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, dict):
        items: list[dict[str, Any]] = []
        for key in ("FILE_VISION", "TITLE_DESC"):
            value = data.get(key)
            if isinstance(value, list):
                items.extend(item for item in value if isinstance(item, dict))
        if items:
            return items
    for key in ("images", "results", "items", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _candidate_from_item(item: dict[str, Any], *, plan: ReportImagePlan, index: int) -> ReportImageCandidate | None:
    url = (
        item.get("url")
        or item.get("image_url")
        or item.get("fileUrl")
        or item.get("src")
        or item.get("thumbnail_url")
    )
    if not url:
        return None
    return ReportImageCandidate(
        image_id=str(item.get("image_id") or item.get("id") or item.get("fileId") or f"img_{index}"),
        title=str(item.get("title") or item.get("caption") or item.get("fileName") or plan.image_query),
        url=str(url),
        description=str(item.get("description") or item.get("summary") or ""),
        source=str(item.get("source") or item.get("source_url") or item.get("fileId") or ""),
        license=str(item.get("license") or item.get("rights") or ""),
        section_title=plan.section_title,
        reason=plan.reason,
    )


def is_report_image_enabled(enabled: bool | None = None) -> bool:
    return settings.report_image_enabled if enabled is None else bool(enabled)


async def plan_report_images(
    outline: dict | None,
    chunks: list[ProvidedChunkInput],
    *,
    enabled: bool | None = None,
) -> list[ReportImagePlan]:
    if not is_report_image_enabled(enabled) or not settings.image_mcp_url:
        return []

    provider = get_provider("doubao")
    messages = [
        {"role": "system", "content": IMAGE_PLAN_SYSTEM},
        {
            "role": "user",
            "content": (
                f"报告大纲：\n{format_outline_for_report(outline)}\n\n"
                f"参考资料：\n{format_chunks(chunks[: settings.rag_top_k])}"
            ),
        },
    ]
    try:
        result = await provider.chat(
            messages,
            model=settings.model_fast,
            temperature=0,
            max_tokens=1024,
        )
        parsed = parse_json_object(result.get("text", ""), {"plans": []})
        raw_plans = parsed.get("plans") if isinstance(parsed, dict) else []
        plans = []
        for item in raw_plans or []:
            if not isinstance(item, dict):
                continue
            try:
                plan = ReportImagePlan.model_validate(item)
            except Exception:
                continue
            if plan.section_title and plan.image_query:
                plans.append(plan)
            if len(plans) >= settings.report_image_max_sections:
                break
        return plans
    except Exception:
        return []


async def _search_one_plan(
    plan: ReportImagePlan,
    *,
    api_key: str | None = None,
    tenant_code: str | None = None,
) -> list[ReportImageCandidate]:
    headers = {"Content-Type": "application/json"}
    final_api_key = api_key if api_key is not None else settings.image_mcp_api_key
    final_tenant_code = tenant_code if tenant_code is not None else settings.image_mcp_tenant_code
    if final_api_key:
        headers["Authorization"] = f"Bearer {final_api_key}"
    if settings.image_mcp_api_type == "media_resources":
        if final_tenant_code:
            headers["X-Tenant-Code"] = final_tenant_code
        body = {
            "searchText": plan.image_query,
            "page": 1,
            "pageSize": settings.report_image_top_k,
        }
    else:
        body = {
            "query": plan.image_query,
            "top_k": settings.report_image_top_k,
            "section_title": plan.section_title,
        }
    async with httpx.AsyncClient(timeout=settings.image_mcp_timeout_seconds, trust_env=False) as client:
        resp = await client.post(settings.image_mcp_url, headers=headers, json=body)
        resp.raise_for_status()
        payload = resp.json()

    candidates = []
    for item in _coerce_items(payload)[: settings.report_image_top_k]:
        candidate = _candidate_from_item(item, plan=plan, index=len(candidates) + 1)
        if candidate:
            candidates.append(candidate)
    return candidates


async def search_report_images(
    plans: list[ReportImagePlan],
    *,
    api_key: str | None = None,
    tenant_code: str | None = None,
) -> list[ReportImageCandidate]:
    if not plans:
        return []
    results = await asyncio.gather(
        *[_search_one_plan(plan, api_key=api_key, tenant_code=tenant_code) for plan in plans],
        return_exceptions=True,
    )
    candidates: list[ReportImageCandidate] = []
    seen_urls: set[str] = set()
    for result in results:
        if isinstance(result, Exception):
            continue
        for candidate in result:
            if candidate.url in seen_urls:
                continue
            seen_urls.add(candidate.url)
            candidate.image_id = f"img_{len(candidates) + 1}"
            candidates.append(candidate)
    return candidates


def format_image_candidates(candidates: list[ReportImageCandidate]) -> str:
    if not candidates:
        return "无"
    rows = []
    for item in candidates:
        rows.append(
            "\n".join(
                [
                    f"[image_id={item.image_id}]",
                    f"适用章节：{item.section_title}",
                    f"标题：{item.title}",
                    f"URL：{item.url}",
                    f"描述：{item.description or '无'}",
                    f"来源：{item.source or '无'}",
                    f"授权：{item.license or '未知'}",
                    f"推荐原因：{item.reason or '无'}",
                ]
            )
        )
    return "\n\n".join(rows)


async def plan_image_insertions(
    outline: dict | None,
    chunks: list[ProvidedChunkInput],
    candidates: list[ReportImageCandidate],
) -> list[ReportImageInsertionPlan]:
    if not candidates:
        return []

    provider = get_provider("doubao")
    messages = [
        {"role": "system", "content": IMAGE_INSERTION_PLAN_SYSTEM},
        {
            "role": "user",
            "content": (
                f"报告大纲：\n{format_outline_for_report(outline)}\n\n"
                f"参考资料：\n{format_chunks(chunks[: settings.rag_top_k])}\n\n"
                f"可用图片候选：\n{format_image_candidates(candidates)}"
            ),
        },
    ]
    try:
        result = await provider.chat(
            messages,
            model=settings.model_fast,
            temperature=0,
            max_tokens=1024,
        )
        parsed = parse_json_object(result.get("text", ""), {"insertions": []})
        raw_insertions = parsed.get("insertions") if isinstance(parsed, dict) else []
        valid_ids = {item.image_id for item in candidates}
        used_ids: set[str] = set()
        used_sections: set[str] = set()
        insertions = []
        for item in raw_insertions or []:
            if not isinstance(item, dict):
                continue
            try:
                insertion = ReportImageInsertionPlan.model_validate(item)
            except Exception:
                continue
            section_key = insertion.section_title.strip()
            if (
                insertion.image_id not in valid_ids
                or insertion.image_id in used_ids
                or section_key in used_sections
                or not insertion.insert_after_topic.strip()
            ):
                continue
            used_ids.add(insertion.image_id)
            used_sections.add(section_key)
            insertions.append(insertion)
        return insertions
    except Exception:
        return []


def format_image_insertion_plans(insertions: list[ReportImageInsertionPlan]) -> str:
    if not insertions:
        return "无"
    rows = []
    for item in insertions:
        rows.append(
            "\n".join(
                [
                    f"[image_id={item.image_id}]",
                    f"目标章节：{item.section_title}",
                    f"插入时机：{item.insert_after_topic}",
                    f"图片说明：{item.caption}",
                    f"规划原因：{item.reason or '无'}",
                ]
            )
        )
    return "\n\n".join(rows)


async def collect_report_images(
    outline: dict | None,
    chunks: list[ProvidedChunkInput],
    *,
    enabled: bool | None = None,
    api_key: str | None = None,
    tenant_code: str | None = None,
) -> list[ReportImageCandidate]:
    plans = await plan_report_images(outline, chunks, enabled=enabled)
    return await search_report_images(plans, api_key=api_key, tenant_code=tenant_code)


def image_candidates_as_dicts(candidates: list[ReportImageCandidate]) -> list[dict[str, Any]]:
    return [json.loads(item.model_dump_json()) for item in candidates]


def image_insertions_as_dicts(insertions: list[ReportImageInsertionPlan]) -> list[dict[str, Any]]:
    return [json.loads(item.model_dump_json()) for item in insertions]
