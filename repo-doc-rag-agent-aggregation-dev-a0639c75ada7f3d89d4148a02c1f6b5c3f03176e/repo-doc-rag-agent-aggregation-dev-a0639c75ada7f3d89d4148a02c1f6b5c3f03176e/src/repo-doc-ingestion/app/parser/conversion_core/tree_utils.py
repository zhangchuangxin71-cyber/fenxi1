from typing import Any, Dict, Iterator, List, Optional, Tuple


class TreeUtil:
    """Iterative helpers for dict-based tree nodes with `nodes` children."""

    @staticmethod
    def children(node: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Return validated dict children from node['nodes']."""
        children = node.get("nodes")
        if not isinstance(children, list):
            return []
        return [c for c in children if isinstance(c, dict)]

    @staticmethod
    def iter_preorder(nodes: List[Dict[str, Any]]) -> Iterator[Tuple[Dict[str, Any], int, Optional[Dict[str, Any]]]]:
        """Iterate nodes in preorder with (node, depth, parent)."""
        stack: List[Tuple[Dict[str, Any], int, Optional[Dict[str, Any]]]] = []
        for node in reversed(nodes):
            if isinstance(node, dict):
                stack.append((node, 1, None))

        while stack:
            node, depth, parent = stack.pop()
            yield node, depth, parent
            children = TreeUtil.children(node)
            for child in reversed(children):
                stack.append((child, depth + 1, node))

    @staticmethod
    def iter_postorder(nodes: List[Dict[str, Any]]) -> Iterator[Tuple[Dict[str, Any], int, Optional[Dict[str, Any]]]]:
        """Iterate nodes in postorder with (node, depth, parent)."""
        stack: List[Tuple[Dict[str, Any], int, Optional[Dict[str, Any]], bool]] = []
        for node in reversed(nodes):
            if isinstance(node, dict):
                stack.append((node, 1, None, False))

        while stack:
            node, depth, parent, visited = stack.pop()
            if visited:
                yield node, depth, parent
                continue

            stack.append((node, depth, parent, True))
            children = TreeUtil.children(node)
            for child in reversed(children):
                stack.append((child, depth + 1, node, False))

    @staticmethod
    def iter_leaves(nodes: List[Dict[str, Any]]) -> Iterator[Tuple[Dict[str, Any], int, Optional[Dict[str, Any]]]]:
        """Iterate leaf nodes only with (node, depth, parent)."""
        for node, depth, parent in TreeUtil.iter_preorder(nodes):
            if not TreeUtil.children(node):
                yield node, depth, parent

    @staticmethod
    def max_depth(nodes: List[Dict[str, Any]]) -> int:
        """Compute maximum tree depth iteratively."""
        max_depth = 0
        for _, depth, _ in TreeUtil.iter_preorder(nodes):
            if depth > max_depth:
                max_depth = depth
        return max_depth

    @staticmethod
    def flatten_subtree_preorder(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Flatten subtree roots into preorder list."""
        out: List[Dict[str, Any]] = []
        stack: List[Dict[str, Any]] = []
        for node in reversed(nodes):
            if isinstance(node, dict):
                stack.append(node)

        while stack:
            node = stack.pop()
            out.append(node)
            children = TreeUtil.children(node)
            for child in reversed(children):
                stack.append(child)
        return out
