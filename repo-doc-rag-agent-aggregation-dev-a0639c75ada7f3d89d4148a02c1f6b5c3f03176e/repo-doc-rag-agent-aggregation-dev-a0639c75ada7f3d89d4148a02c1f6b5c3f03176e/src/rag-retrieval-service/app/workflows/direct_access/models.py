from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DirectToolCall(StrictModel):
    tool_name: Literal[
        "get_docs_metainfo",
        "get_docs_description",
        "get_content_of_pages",
        "get_content_of_chapters",
        "view_doc_title_tree",
    ]
    doc_ids: list[str] = Field(default_factory=list)
    page_numbers_by_doc: dict[str, list[int]] = Field(default_factory=dict)
    chapter_numbers_by_doc: dict[str, list[int]] = Field(default_factory=dict)
    node_ids_by_doc: dict[str, list[str]] = Field(default_factory=dict)
    metainfo_fields: list[Literal["doc_name", "page_count", "chapter_count"]] = Field(
        default_factory=list
    )
    tree_level: int = Field(default=2, ge=0, le=32)
    include_node_id: bool = False

    @model_validator(mode="after")
    def validate_resource_parameters(self) -> DirectToolCall:
        if (
            self.tool_name
            in {
                "get_docs_metainfo",
                "get_docs_description",
                "view_doc_title_tree",
            }
            and not self.doc_ids
        ):
            raise ValueError("direct tool call requires at least one document ID")
        if self.tool_name == "get_docs_metainfo" and not self.metainfo_fields:
            self.metainfo_fields = ["doc_name"]
        if self.tool_name == "get_content_of_pages":
            if not self.page_numbers_by_doc or any(
                not pages or any(int(page) <= 0 for page in pages)
                for pages in self.page_numbers_by_doc.values()
            ):
                raise ValueError("page tool call requires positive page numbers")
        if self.tool_name == "get_content_of_chapters":
            if not self.chapter_numbers_by_doc and not self.node_ids_by_doc:
                raise ValueError("chapter tool call requires chapter or node IDs")
            if any(
                not chapters or any(int(chapter) <= 0 for chapter in chapters)
                for chapters in self.chapter_numbers_by_doc.values()
            ):
                raise ValueError("chapter tool call requires positive chapter numbers")
            if any(
                not node_ids or any(not str(node_id).strip() for node_id in node_ids)
                for node_ids in self.node_ids_by_doc.values()
            ):
                raise ValueError("chapter tool call requires non-empty node IDs")
        return self


class DirectPlan(StrictModel):
    calls: list[DirectToolCall] = Field(min_length=1)


class DirectPlanError(ValueError):
    pass
