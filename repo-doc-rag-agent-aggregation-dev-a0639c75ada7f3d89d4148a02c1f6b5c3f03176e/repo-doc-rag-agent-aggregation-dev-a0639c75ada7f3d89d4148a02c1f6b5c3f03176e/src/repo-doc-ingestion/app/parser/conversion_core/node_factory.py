from typing import Any, Dict, Optional

from .text_utils import clean_text


class NodeFactory:
    """Build normalized node dictionaries for all document parsers.

    Required semantics:
    - `level` is always an int >= 1
    - `node_id` and `text` are controlled by parser-level feature flags
    - empty/whitespace-only text returns `None` to prevent dirty nodes
    """

    def __init__(self, add_node_id: bool, add_node_text: bool) -> None:
        self.add_node_id = bool(add_node_id)
        self.add_node_text = bool(add_node_text)

    def create(
        self,
        *,
        node_id: int,
        text: str,
        page_number: Optional[int],
        level: int,
        **kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        """Create one normalized node payload or return None for empty text."""
        cleaned_text = clean_text(text)
        if not cleaned_text:
            return None

        node: Dict[str, Any] = {
            "node_id": node_id if self.add_node_id else None,
            "text": cleaned_text if self.add_node_text else None,
            "page_number": page_number,
            "level": max(1, int(level)),
        }
        node.update(kwargs)
        return node
