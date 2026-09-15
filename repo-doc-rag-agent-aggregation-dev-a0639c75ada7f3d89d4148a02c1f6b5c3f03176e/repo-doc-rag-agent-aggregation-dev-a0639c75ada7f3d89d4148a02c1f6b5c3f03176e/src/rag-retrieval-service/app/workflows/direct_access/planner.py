from __future__ import annotations

import json
import re

from app.db.repositories import DocumentProfile
from app.llm.gateway import LLMCallError, LLMGateway, LLMRequest, LLMToolCall
from app.llm.tools import strict_function_tool
from app.workflows.direct_access.models import DirectPlan, DirectPlanError, DirectToolCall

_CHINESE_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def _number(value: str) -> int:
    if value.isdigit():
        return int(value)
    if value == "十":
        return 10
    if "十" in value:
        left, right = value.split("十", 1)
        tens = _CHINESE_DIGITS.get(left, 1) if left else 1
        ones = _CHINESE_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(value) == 1 and value in _CHINESE_DIGITS:
        return _CHINESE_DIGITS[value]
    raise DirectPlanError("unsupported Chinese number")


def _target_documents(query: str, documents: list[DocumentProfile]) -> list[DocumentProfile]:
    explicit = [
        document for document in documents if document.doc_name and document.doc_name in query
    ]
    if explicit:
        return explicit
    if len(documents) == 1:
        return documents
    raise DirectPlanError("the query does not identify a unique document")


class RuleDirectPlanner:
    """Parses only explicit resource references; ambiguous references are rejected."""

    def plan(self, *, query: str, documents: list[DocumentProfile]) -> DirectPlan:
        targets = _target_documents(query, documents)
        doc_ids = [document.doc_id for document in targets]
        if any(marker in query for marker in ("有几页", "一共几页", "多少页", "页数")):
            return DirectPlan(
                calls=[
                    DirectToolCall(
                        tool_name="get_docs_metainfo",
                        doc_ids=doc_ids,
                        metainfo_fields=["doc_name", "page_count"],
                    )
                ]
            )
        page_matches = re.findall(r"第\s*([0-9零一二两三四五六七八九十]+)\s*页", query)
        if page_matches:
            pages = [_number(match) for match in page_matches]
            return DirectPlan(
                calls=[
                    DirectToolCall(
                        tool_name="get_content_of_pages",
                        page_numbers_by_doc={doc_id: pages for doc_id in doc_ids},
                    )
                ]
            )
        chapter_matches = re.findall(r"第\s*([0-9零一二两三四五六七八九十]+)\s*章", query)
        if chapter_matches:
            chapters = [_number(match) for match in chapter_matches]
            return DirectPlan(
                calls=[
                    DirectToolCall(
                        tool_name="get_content_of_chapters",
                        chapter_numbers_by_doc={doc_id: chapters for doc_id in doc_ids},
                    )
                ]
            )
        if any(marker in query for marker in ("目录", "章节结构", "标题结构", "有几章")):
            return DirectPlan(
                calls=[
                    DirectToolCall(
                        tool_name="view_doc_title_tree",
                        doc_ids=doc_ids,
                        tree_level=3,
                        include_node_id=False,
                    )
                ]
            )
        if any(marker in query for marker in ("简要总结", "简要概览", "摘要", "简介")):
            return DirectPlan(
                calls=[DirectToolCall(tool_name="get_docs_description", doc_ids=doc_ids)]
            )
        raise DirectPlanError("query does not contain a deterministic direct resource reference")


DIRECT_PLANNER_PROMPT = """# 角色与任务
你是直接资源工具规划器。你只负责把用户明确指定的文档资源请求映射为工具调用，不回答问题。

# 输入说明
用户消息包含问题组、候选文档 ID、文档名、页数和章节数。只能引用输入中出现的 doc_id。

# 工具使用规则
- 查询页数、章节数等文档元信息时调用 get_docs_metainfo。
- 获取数据库中已有的文档简要摘要时调用 get_docs_description。
- 用户明确指定页码时调用 get_content_of_pages。
- 用户明确指定章节序号时调用 get_content_of_chapters；不要猜测 node_id。
- 用户要求目录、章节结构或标题结构时调用 view_doc_title_tree。
- view_doc_title_tree 的 include_node_id 必须为 false；内部节点 ID 不能进入用户证据。
- 一个问题组需要多种资源时可以并行调用多个工具。

# 禁止事项
- 不得扩大候选文档范围，不得编造 doc_id、页码或章节序号。
- 不得生成答案、理由、摘要或改写正文。
- 不得使用自然语言描述待执行的调用，必须直接调用工具。

# 示例
- “查看年报第 12 页”应调用 get_content_of_pages。
- “这篇文档有几页”应调用 get_docs_metainfo。
- “查看这篇文档的目录”应调用 view_doc_title_tree。

# 输出约束
只发起完成当前问题所必需的工具调用。
"""


_DOC_IDS = {
    "type": "array",
    "description": "需要访问的候选文档 ID 列表，只能使用输入中提供的 ID。",
    "items": {"type": "string", "description": "单篇候选文档的唯一 ID。"},
}
_META_TYPES = {
    "type": "array",
    "description": "需要返回的文档元信息字段，只选择回答问题所需的字段。",
    "items": {
        "type": "string",
        "enum": ["doc_name", "page_count", "chapter_count"],
        "description": "文档名、总页数或章节数。",
    },
}
_PAGE_DOCUMENTS = {
    "type": "array",
    "description": "按文档列出的指定页访问请求。",
    "items": {
        "type": "object",
        "description": "一篇文档及其需要返回的页码。",
        "properties": {
            "doc_id": {"type": "string", "description": "候选文档的唯一 ID。"},
            "page_numbers": {
                "type": "array",
                "description": "用户明确要求查看的正整数页码。",
                "items": {"type": "integer", "minimum": 1, "description": "文档页码。"},
            },
        },
        "required": ["doc_id", "page_numbers"],
        "additionalProperties": False,
    },
}
_CHAPTER_DOCUMENTS = {
    "type": "array",
    "description": "按文档列出的指定章节访问请求。",
    "items": {
        "type": "object",
        "description": "一篇文档及其需要返回的章节序号。",
        "properties": {
            "doc_id": {"type": "string", "description": "候选文档的唯一 ID。"},
            "chapter_numbers": {
                "type": "array",
                "description": "用户明确要求查看的正整数章节序号。",
                "items": {"type": "integer", "minimum": 1, "description": "章节序号。"},
            },
        },
        "required": ["doc_id", "chapter_numbers"],
        "additionalProperties": False,
    },
}

DIRECT_TOOLS = [
    strict_function_tool(
        name="get_docs_metainfo",
        description="读取指定文档的文档名、总页数或章节数，并生成元信息证据。",
        properties={"doc_ids": _DOC_IDS, "return_metainfo_types": _META_TYPES},
        required=["doc_ids", "return_metainfo_types"],
    ),
    strict_function_tool(
        name="get_docs_description",
        description="读取指定文档在数据库中已有的简要摘要，每篇文档返回一个独立证据块。",
        properties={"doc_ids": _DOC_IDS},
        required=["doc_ids"],
    ),
    strict_function_tool(
        name="get_content_of_pages",
        description="读取用户明确指定页码的原始页面内容。",
        properties={"documents": _PAGE_DOCUMENTS},
        required=["documents"],
    ),
    strict_function_tool(
        name="get_content_of_chapters",
        description="根据用户指定的章节序号定位章节，并返回对应页面原文。",
        properties={"documents": _CHAPTER_DOCUMENTS},
        required=["documents"],
    ),
    strict_function_tool(
        name="view_doc_title_tree",
        description="构造指定文档的标题树，用于回答目录或章节结构问题。",
        properties={
            "doc_ids": _DOC_IDS,
            "level": {
                "type": "integer",
                "minimum": 0,
                "maximum": 32,
                "description": "标题树最多展开到的层级；0 仅包含根节点。",
            },
            "include_node_id": {
                "type": "boolean",
                "description": "是否在标题树证据中展示内部章节节点 ID。",
            },
        },
        required=["doc_ids", "level", "include_node_id"],
    ),
]


class LLMDirectPlanner:
    def __init__(self, *, gateway: LLMGateway, model: str, max_tokens: int) -> None:
        self.gateway = gateway
        self.model = model
        self.max_tokens = max(1, int(max_tokens))

    async def plan(
        self, *, request_id: str, group_ref: str, query: str, documents: list[DocumentProfile]
    ) -> DirectPlan:
        payload = json.dumps(
            {
                "group_ref": group_ref,
                "query": query,
                "documents": [
                    {
                        "doc_id": document.doc_id,
                        "doc_name": document.doc_name,
                        "page_count": document.page_count,
                        "chapter_count": document.node_count,
                    }
                    for document in documents
                ],
            },
            ensure_ascii=False,
        )
        result = await self.gateway.complete_json(
            LLMRequest(
                request_id=request_id,
                phase="direct_tool_planner",
                messages=[
                    {"role": "system", "content": DIRECT_PLANNER_PROMPT},
                    {"role": "user", "content": payload},
                ],
                model=self.model,
                max_tokens=self.max_tokens,
                tools=DIRECT_TOOLS,
                tool_choice="required",
                parallel_tool_calls=True,
            )
        )
        try:
            plan = DirectPlan(calls=[_to_direct_call(call) for call in result.tool_calls])
        except Exception as exc:
            raise LLMCallError(
                "invalid direct tool plan", error_category="invalid_response"
            ) from exc
        allowed_ids = {document.doc_id for document in documents}
        referenced = {
            doc_id
            for call in plan.calls
            for doc_id in [
                *call.doc_ids,
                *call.page_numbers_by_doc,
                *call.chapter_numbers_by_doc,
                *call.node_ids_by_doc,
            ]
        }
        if referenced - allowed_ids:
            raise LLMCallError(
                "direct tool plan referenced unknown document", error_category="invalid_response"
            )
        return plan


def _to_direct_call(call: LLMToolCall) -> DirectToolCall:
    arguments = call.arguments
    if call.name == "get_docs_metainfo":
        return DirectToolCall(
            tool_name=call.name,
            doc_ids=arguments["doc_ids"],
            metainfo_fields=arguments["return_metainfo_types"],
        )
    if call.name == "get_docs_description":
        return DirectToolCall(tool_name=call.name, doc_ids=arguments["doc_ids"])
    if call.name == "get_content_of_pages":
        return DirectToolCall(
            tool_name=call.name,
            page_numbers_by_doc={
                item["doc_id"]: item["page_numbers"] for item in arguments["documents"]
            },
        )
    if call.name == "get_content_of_chapters":
        return DirectToolCall(
            tool_name=call.name,
            chapter_numbers_by_doc={
                item["doc_id"]: item["chapter_numbers"] for item in arguments["documents"]
            },
        )
    if call.name == "view_doc_title_tree":
        return DirectToolCall(
            tool_name=call.name,
            doc_ids=arguments["doc_ids"],
            tree_level=arguments["level"],
            include_node_id=False,
        )
    raise ValueError(f"unsupported direct tool: {call.name}")
