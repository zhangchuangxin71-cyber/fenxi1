from __future__ import annotations

import re
from typing import Any

from app.workflows.classification.models import DocumentCluster
from app.workflows.keyword_ranking import tokenize_query

_QUERY_REF_PATTERN = re.compile(r"q_[0-9]{3,}\Z")


def validate_query_refs(query_refs: list[str]) -> None:
    if not query_refs:
        raise ValueError("query refs must not be empty")
    if len(set(query_refs)) != len(query_refs):
        raise ValueError("query refs must be unique")
    if any(not _QUERY_REF_PATTERN.fullmatch(ref) for ref in query_refs):
        raise ValueError("query refs must match q_<at least three digits>")


def binary_decision_schema(
    query_refs: list[str], *, schema_name: str = "binary_query_decisions"
) -> dict[str, Any]:
    validate_query_refs(query_refs)
    return {
        "type": "json_schema",
        "json_schema": {
            "name": schema_name,
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "decisions": {
                        "type": "object",
                        "description": "每个 query ref 对应的二分类结果。",
                        "properties": {
                            ref: {
                                "type": "boolean",
                                "description": f"{ref} 是否满足当前分类器的 true 条件。",
                            }
                            for ref in query_refs
                        },
                        "required": list(query_refs),
                        "additionalProperties": False,
                    }
                },
                "required": ["decisions"],
                "additionalProperties": False,
            },
        },
    }


def validate_binary_decisions(payload: dict[str, Any], query_refs: list[str]) -> dict[str, bool]:
    validate_query_refs(query_refs)
    if not isinstance(payload, dict) or set(payload) != {"decisions"}:
        raise ValueError("response must contain only the decisions object")
    decisions = payload["decisions"]
    if not isinstance(decisions, dict) or set(decisions) != set(query_refs):
        raise ValueError("decision keys must exactly match the requested query refs")
    if any(type(value) is not bool for value in decisions.values()):
        raise ValueError("every decision must be a JSON boolean")
    return {ref: decisions[ref] for ref in query_refs}


def query_rewrite_schema() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "rewritten_retrieval_queries",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "queries": {
                        "type": "array",
                        "description": "从原问题拆分并必要改写后的独立检索问题。",
                        "items": {
                            "type": "string",
                            "description": "脱离其他子问题也能独立用于检索的问题。",
                        },
                    }
                },
                "required": ["queries"],
                "additionalProperties": False,
            },
        },
    }


def validate_rewritten_queries(payload: dict[str, Any], original_query: str) -> list[str]:
    if not isinstance(payload, dict) or set(payload) != {"queries"}:
        raise ValueError("rewrite response must contain only queries")
    raw_queries = payload["queries"]
    if not isinstance(raw_queries, list) or not raw_queries:
        raise ValueError("rewritten queries must be a non-empty array")
    original_terms = set(tokenize_query(original_query))
    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw_queries:
        if not isinstance(item, str):
            raise ValueError("every rewritten query must be a string")
        question = item.strip()
        key = re.sub(r"\s+", "", question.casefold())
        if len(key) < 2:
            raise ValueError("rewritten queries must not be blank or trivial")
        if key in seen:
            raise ValueError("rewritten queries must not repeat")
        if not (set(tokenize_query(question)) & original_terms):
            raise ValueError("rewrite introduced a question unrelated to the original query")
        seen.add(key)
        normalized.append(question)
    if sum(len(question) for question in normalized) > 16000:
        raise ValueError("rewritten queries exceed the total character limit")
    return normalized


def document_grouping_schema(query_refs: list[str]) -> dict[str, Any]:
    validate_query_refs(query_refs)
    group_item = {
        "type": "object",
        "properties": {
            "query_refs": {
                "type": "array",
                "description": "询问同一目标文档或同一组目标文档的 query refs。",
                "items": {
                    "type": "string",
                    "enum": list(query_refs),
                    "description": "输入中已有的 query ref。",
                },
            },
            "target_docs_description": {
                "type": "string",
                "description": "目标文档身份、主题或类型，不包含答案和执行动作。",
            },
            "target_docs_keywords": {
                "type": "array",
                "description": "可能出现在文档名或数据库摘要中的身份锚点。",
                "items": {
                    "type": "string",
                    "description": "文档名称、别名、主体名称或文档类型词。",
                },
            },
        },
        "required": ["query_refs", "target_docs_description", "target_docs_keywords"],
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "target_document_groups",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "document_groups": {
                        "type": "array",
                        "description": "按目标文档身份划分的问题组。",
                        "items": group_item,
                    }
                },
                "required": ["document_groups"],
                "additionalProperties": False,
            },
        },
    }


def validate_document_groups(
    payload: dict[str, Any], query_refs: list[str]
) -> list[DocumentCluster]:
    validate_query_refs(query_refs)
    if not isinstance(payload, dict) or set(payload) != {"document_groups"}:
        raise ValueError("response must contain only document_groups")
    raw_groups = payload["document_groups"]
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ValueError("document_groups must be a non-empty array")
    clusters: list[DocumentCluster] = []
    seen: list[str] = []
    required_keys = {"query_refs", "target_docs_description", "target_docs_keywords"}
    allowed_refs = set(query_refs)
    for raw in raw_groups:
        if not isinstance(raw, dict) or set(raw) != required_keys:
            raise ValueError("every document group must use the exact contract")
        refs = raw["query_refs"]
        description = raw["target_docs_description"]
        keywords = raw["target_docs_keywords"]
        if (
            not isinstance(refs, list)
            or not refs
            or any(not isinstance(ref, str) or ref not in allowed_refs for ref in refs)
        ):
            raise ValueError("document group refs must be non-empty and known")
        if len(set(refs)) != len(refs) or any(ref in seen for ref in refs):
            raise ValueError("query refs must not repeat within or across document groups")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("target_docs_description must not be blank")
        if not isinstance(keywords, list) or any(not isinstance(item, str) for item in keywords):
            raise ValueError("target_docs_keywords must be a string array")
        normalized_keywords = tuple(
            dict.fromkeys(item.strip() for item in keywords if item.strip())
        )
        seen.extend(refs)
        clusters.append(
            DocumentCluster(
                query_refs=tuple(refs),
                target_docs_description=description.strip(),
                target_docs_keywords=normalized_keywords,
            )
        )
    if set(seen) != allowed_refs or len(seen) != len(query_refs):
        raise ValueError("document groups must form an exact partition of query refs")
    return clusters
