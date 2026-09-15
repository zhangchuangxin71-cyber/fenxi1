import json
import PyPDF2

try:
    from .utils import get_number_of_pages, remove_fields
except ImportError:
    from utils import get_number_of_pages, remove_fields


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_pages(pages: str) -> list[int]:
    """Parse a pages string like '5-7', '3,8', or '12' into a sorted list of ints."""
    result = []
    for part in pages.split(','):
        part = part.strip()
        if '-' in part:
            start, end = int(part.split('-', 1)[0].strip()), int(part.split('-', 1)[1].strip())
            if start > end:
                raise ValueError(f"Invalid range '{part}': start must be <= end")
            result.extend(range(start, end + 1))
        else:
            result.append(int(part))
    return sorted(set(result))


def _count_pages(doc_info: dict) -> int:
    """Return total page count for a PDF document."""
    if doc_info.get('page_count'):
        return doc_info['page_count']
    if doc_info.get('pages'):
        return len(doc_info['pages'])
    return get_number_of_pages(doc_info['path'])


def _get_pdf_page_content(doc_info: dict, page_nums: list[int]) -> list[dict]:
    """Extract text for specific PDF pages (1-indexed). Prefer cached pages, fallback to PDF."""
    cached_pages = doc_info.get('pages')
    if cached_pages:
        page_map = {p['page']: p['content'] for p in cached_pages}
        return [
            {'page': p, 'content': page_map[p]}
            for p in page_nums if p in page_map
        ]
    path = doc_info['path']
    with open(path, 'rb') as f:
        pdf_reader = PyPDF2.PdfReader(f)
        total = len(pdf_reader.pages)
        valid_pages = [p for p in page_nums if 1 <= p <= total]
        return [
            {'page': p, 'content': pdf_reader.pages[p - 1].extract_text() or ''}
            for p in valid_pages
        ]


def _get_md_page_content(doc_info: dict, page_nums: list[int]) -> list[dict]:
    """
    For Markdown documents, prefer page-index based retrieval using structure start/end indices.
    Fallback to legacy line_num behavior when start/end are unavailable.
    """
    if not page_nums:
        return []

    requested_pages = sorted(set(int(p) for p in page_nums if int(p) > 0))
    requested_set = set(requested_pages)
    min_req, max_req = min(requested_pages), max(requested_pages)
    results = []
    seen = set()

    def _append_result(page_value, content):
        key = (int(page_value), content or "")
        if key in seen:
            return
        seen.add(key)
        results.append({'page': int(page_value), 'content': content or ''})

    def _traverse(nodes):
        for node in nodes:
            start_idx = node.get('start_index')
            end_idx = node.get('end_index')
            if isinstance(start_idx, int):
                node_end = end_idx if isinstance(end_idx, int) and end_idx >= start_idx else start_idx
                if start_idx <= max_req and node_end >= min_req:
                    # Fast overlap check, then strict filter against requested set.
                    if any((p in requested_set) for p in range(start_idx, node_end + 1)):
                        _append_result(start_idx, node.get('text', ''))
            else:
                # Legacy fallback: treat request as line-range-like behavior.
                ln = node.get('line_num')
                if isinstance(ln, int) and min_req <= ln <= max_req:
                    _append_result(ln, node.get('text', ''))
            if node.get('nodes'):
                _traverse(node['nodes'])

    _traverse(doc_info.get('structure', []))
    results.sort(key=lambda x: x['page'])
    return results


# ── Tool functions ────────────────────────────────────────────────────────────

def get_document(documents: dict, doc_id: str) -> str:
    """Return JSON with document metadata: doc_id, doc_name, doc_description, type, status, page_count (PDF) or line_count (Markdown)."""
    doc_info = documents.get(doc_id)
    if not doc_info:
        return json.dumps({'error': f'Document {doc_id} not found'})
    result = {
        'doc_id': doc_id,
        'doc_name': doc_info.get('doc_name', ''),
        'doc_description': doc_info.get('doc_description', ''),
        'type': doc_info.get('type', ''),
        'status': 'completed',
    }
    if doc_info.get('type') == 'pdf':
        result['page_count'] = _count_pages(doc_info)
    else:
        result['line_count'] = doc_info.get('line_count', 0)
    return json.dumps(result)


def get_document_structure(documents: dict, doc_id: str) -> str:
    """Return tree structure JSON with text fields removed (saves tokens)."""
    doc_info = documents.get(doc_id)
    if not doc_info:
        return json.dumps({'error': f'Document {doc_id} not found'})
    structure = doc_info.get('structure', [])
    structure_no_text = remove_fields(structure, fields=['text'])
    return json.dumps(structure_no_text, ensure_ascii=False)


def get_page_content(documents: dict, doc_id: str, pages: str) -> str:
    """
    Retrieve page content for a document.

    pages format: '5-7', '3,8', or '12'
    For PDF: pages are physical page numbers (1-indexed).
    For Markdown: pages are line numbers corresponding to node headers.

    Returns JSON list of {'page': int, 'content': str}.
    """
    doc_info = documents.get(doc_id)
    if not doc_info:
        return json.dumps({'error': f'Document {doc_id} not found'})

    try:
        page_nums = _parse_pages(pages)
    except (ValueError, AttributeError) as e:
        return json.dumps({'error': f'Invalid pages format: {pages!r}. Use "5-7", "3,8", or "12". Error: {e}'})

    try:
        if doc_info.get('type') == 'pdf':
            content = _get_pdf_page_content(doc_info, page_nums)
        else:
            content = _get_md_page_content(doc_info, page_nums)
    except Exception as e:
        return json.dumps({'error': f'Failed to read page content: {e}'})

    return json.dumps(content, ensure_ascii=False)
