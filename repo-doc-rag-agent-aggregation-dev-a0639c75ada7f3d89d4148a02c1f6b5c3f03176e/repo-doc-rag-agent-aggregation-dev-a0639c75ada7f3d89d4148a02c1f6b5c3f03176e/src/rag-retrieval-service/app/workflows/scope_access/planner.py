from __future__ import annotations

import json
import re

from app.llm.gateway import LLMCallError, LLMGateway, LLMRequest
from app.llm.tools import strict_function_tool
from app.workflows.scope_access.models import ScopePlanError, ScopeToolPlan

SCOPE_PLANNER_PROMPT = """# 角色与任务
你是请求范围元信息工具规划器。你只选择一个工具生成检索证据，不回答用户问题。

# 输入说明
用户消息包含当前 scope 子问题、用户原始问题和当前请求范围内的文档数量。
原始问题只用于恢复子问题改写时可能丢失的列举数量约束；工具只能访问这个既定范围。

# 工具使用规则
- 用户询问可见文档、文档数量、文档名、页数或章节数时，调用 get_docs_metainfo。
- 用户要求对当前范围内的全部文档做简要概览时，调用 get_docs_description。
- 用户询问“这些文档主要讲了什么”或“主要内容是什么”时，调用 get_docs_description。
- 调用 get_docs_metainfo 时，如果用户明确要求列举 N 篇，enumeration_limit 必须填 N；
  用户没有明确数量时，enumeration_limit 必须填 10。实际可列举数量由工具按请求范围自动限制。

# 禁止事项
- 不得请求页面正文，不得请求当前范围之外的文档。
- 不得生成答案、理由或工具调用说明。

# 示例
- “你能看到哪些文档，各有几页”调用 get_docs_metainfo。
- “简要总结上述所有文档”调用 get_docs_description。
- “这些文档主要讲了什么”调用 get_docs_description。

# 输出约束
必须且只能调用一个工具。
"""

SCOPE_TOOLS = [
    strict_function_tool(
        name="get_docs_metainfo",
        description="列举当前请求范围内若干文档的必要元信息。",
        properties={
            "return_metainfo_types": {
                "type": "array",
                "description": "需要列举的元信息字段，只选择回答问题所需的字段。",
                "items": {
                    "type": "string",
                    "enum": ["doc_name", "page_count", "chapter_count"],
                    "description": "文档名、总页数或章节数。",
                },
            },
            "enumeration_limit": {
                "type": "integer",
                "minimum": 1,
                "description": "需要列举的文档数量；用户未明确数量时必须填 10。",
            },
        },
        required=["return_metainfo_types", "enumeration_limit"],
    ),
    strict_function_tool(
        name="get_docs_description",
        description="列举当前请求范围内若干文档在数据库中已有的简要摘要。",
        properties={},
        required=[],
    ),
]


class LLMScopePlanner:
    def __init__(self, *, gateway: LLMGateway, model: str, max_tokens: int) -> None:
        self.gateway = gateway
        self.model = model
        self.max_tokens = max(1, int(max_tokens))

    async def plan(
        self,
        *,
        request_id: str,
        query: str,
        original_query: str,
        scope_count: int,
    ) -> ScopeToolPlan:
        result = await self.gateway.complete_json(
            LLMRequest(
                request_id=request_id,
                phase="scope_tool_planner",
                messages=[
                    {"role": "system", "content": SCOPE_PLANNER_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "scope_query": query,
                                "original_query": original_query,
                                "scope_document_count": scope_count,
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                model=self.model,
                max_tokens=self.max_tokens,
                tools=SCOPE_TOOLS,
                tool_choice="required",
                parallel_tool_calls=False,
            )
        )
        try:
            if len(result.tool_calls) != 1:
                raise ValueError("scope planner must return exactly one tool call")
            call = result.tool_calls[0]
            if call.name == "get_docs_description":
                return ScopeToolPlan(tool_name=call.name)
            if call.name == "get_docs_metainfo":
                return ScopeToolPlan(
                    tool_name=call.name,
                    metainfo_fields=call.arguments["return_metainfo_types"],
                    enumeration_limit=call.arguments["enumeration_limit"],
                )
            raise ValueError(f"unsupported scope tool: {call.name}")
        except Exception as exc:
            raise LLMCallError(
                "invalid scope tool plan", error_category="invalid_response"
            ) from exc


class RuleScopePlanner:
    def plan(self, *, query: str, original_query: str = "") -> ScopeToolPlan:
        if any(
            marker in query
            for marker in ("总结", "概览", "摘要", "介绍上述", "主要讲", "主要内容", "大致讲")
        ):
            return ScopeToolPlan(tool_name="get_docs_description")
        if any(marker in query for marker in ("文档", "多少", "数量", "页", "章", "章节")):
            count_match = re.search(r"(\d+)\s*篇", original_query or query)
            enumeration_limit = int(count_match.group(1)) if count_match else 10
            fields = ["doc_name"]
            if any(marker in query for marker in ("页", "页数")):
                fields.append("page_count")
            if any(marker in query for marker in ("章", "章节")):
                fields.append("chapter_count")
            return ScopeToolPlan(
                tool_name="get_docs_metainfo",
                metainfo_fields=fields,
                enumeration_limit=max(1, enumeration_limit),
            )
        raise ScopePlanError("scope query does not match a deterministic tool")
