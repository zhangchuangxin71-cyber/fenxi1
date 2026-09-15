from __future__ import annotations

import ast
import inspect

from app.graph import builder
from app.rendering.wechat_layout.contracts import LayoutEvent


def test_business_nodes_emit_node_activity_only_through_traced_wrapper() -> None:
    tree = ast.parse(inspect.getsource(builder))
    node_activity_owners: list[str] = []

    class ActivityVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.functions: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Name) and node.func.id == "activity":
                kind = next((item.value for item in node.keywords if item.arg == "kind"), None)
                if isinstance(kind, ast.Constant) and kind.value == "node":
                    node_activity_owners.append(self.functions[-1])
            self.generic_visit(node)

    ActivityVisitor().visit(tree)

    assert set(node_activity_owners) == {"traced"}


def test_public_layout_activity_does_not_expose_image_urls_or_validation_details() -> None:
    image_summary = builder._layout_public_summary(
        LayoutEvent(
            stage="render_markdown",
            status="completed",
            details={
                "theme_id": "professional-clean",
                "html_chars": 1234,
                "inserted_images": [
                    {"url": "https://example.com/private-signed-image", "caption": "内部图注"}
                ],
            },
        )
    )
    validation_summary = builder._layout_public_summary(
        LayoutEvent(
            stage="validate_html",
            status="completed",
            details={
                "theme_id": "professional-clean",
                "valid": True,
                "errors": [],
                "warnings": [{"code": "DETAIL", "details": {"source": "internal"}}],
                "metrics": {"source_chars": 5000},
            },
        )
    )

    assert image_summary == {"theme_id": "professional-clean", "image_count": 1}
    assert validation_summary == {
        "theme_id": "professional-clean",
        "valid": True,
        "error_count": 0,
        "warning_count": 1,
    }
