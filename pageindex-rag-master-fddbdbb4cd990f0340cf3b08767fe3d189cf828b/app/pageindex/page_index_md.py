import asyncio
import json
import re
import os
try:
    from .utils import *
except:
    from utils import *


def _parse_page_markers(markdown_lines):
    """Parse explicit page markers like [[PAGE 12]] and return (page_num, line_no) tuples."""
    markers = []
    pattern = re.compile(r"^\s*\[\[PAGE\s+(\d+)\]\]\s*$", flags=re.IGNORECASE)
    for i, line in enumerate(markdown_lines, 1):
        m = pattern.match((line or "").strip())
        if m:
            markers.append((int(m.group(1)), i))
    return markers


def _strip_page_markers_from_text(text):
    """Remove explicit markdown page marker lines like [[PAGE 12]] from text payloads."""
    if not text:
        return ''
    return re.sub(r"(?im)^\s*\[\[PAGE\s+\d+\]\]\s*\n?", "", text).strip()


def _build_line_to_page_mapper(markdown_lines, lines_per_page=120):
    """
    Build a mapper that converts markdown line number -> page number.
    Priority:
    1) explicit [[PAGE X]] markers
    2) fixed-size synthetic pagination fallback
    """
    markers = _parse_page_markers(markdown_lines)
    if markers:
        spans = []
        for idx, (page_num, start_line) in enumerate(markers):
            end_line = markers[idx + 1][1] - 1 if idx + 1 < len(markers) else len(markdown_lines)
            spans.append((page_num, start_line, end_line))

        def _line_to_page(line_num):
            if not isinstance(line_num, int) or line_num <= 0:
                return None
            for page_num, start_line, end_line in spans:
                if start_line <= line_num <= end_line:
                    return page_num
            return spans[-1][0] if spans else None

        return _line_to_page

    lines_per_page = max(1, int(lines_per_page))

    def _line_to_page(line_num):
        if not isinstance(line_num, int) or line_num <= 0:
            return None
        return ((line_num - 1) // lines_per_page) + 1

    return _line_to_page


async def get_node_summary(node, summary_token_threshold=800, model=None):
    node_text = node.get('text')
    num_tokens = count_tokens(node_text, model=model)
    if num_tokens < summary_token_threshold:
        return node_text
    else:
        return await generate_node_summary(node, model=model)


async def generate_summaries_for_structure_md(structure, summary_token_threshold, model=None):
    nodes = structure_to_list(structure)
    concurrency = get_summary_concurrency()
    factories = [
        lambda node=node: get_node_summary(node, summary_token_threshold=summary_token_threshold, model=model)
        for node in nodes
    ]
    summaries = await gather_with_concurrency(factories, concurrency)
    
    for node, summary in zip(nodes, summaries):
        if not node.get('nodes'):
            node['summary'] = summary
        else:
            node['prefix_summary'] = summary
    return structure


def extract_nodes_from_markdown(markdown_content):
    header_pattern = r'^(#{1,6})\s+(.+)$'
    code_block_pattern = r'^```'
    node_list = []
    
    lines = markdown_content.split('\n')
    in_code_block = False
    
    for line_num, line in enumerate(lines, 1):
        stripped_line = line.strip()
        
        # Check for code block delimiters (triple backticks)
        if re.match(code_block_pattern, stripped_line):
            in_code_block = not in_code_block
            continue
        
        # Skip empty lines
        if not stripped_line:
            continue
        
        # Only look for headers when not inside a code block
        if not in_code_block:
            match = re.match(header_pattern, stripped_line)
            if match:
                title = match.group(2).strip()
                node_list.append({'node_title': title, 'line_num': line_num})

    return node_list, lines


def extract_node_text_content(node_list, markdown_lines):    
    all_nodes = []
    for node in node_list:
        line_content = markdown_lines[node['line_num'] - 1]
        header_match = re.match(r'^(#{1,6})', line_content)
        
        if header_match is None:
            print(f"Warning: Line {node['line_num']} does not contain a valid header: '{line_content}'")
            continue
            
        processed_node = {
            'title': node['node_title'],
            'line_num': node['line_num'],
            'level': len(header_match.group(1))
        }
        all_nodes.append(processed_node)
    
    for i, node in enumerate(all_nodes):
        start_line = node['line_num'] - 1
        if i + 1 < len(all_nodes):
            end_line = all_nodes[i + 1]['line_num'] - 1
        else:
            end_line = len(markdown_lines)

        node['start_line'] = start_line + 1
        node['end_line'] = max(start_line + 1, end_line)
        raw_text = '\n'.join(markdown_lines[start_line:end_line]).strip()
        node['text'] = _strip_page_markers_from_text(raw_text)
    return all_nodes


def attach_page_indices(node_list, line_to_page):
    """Attach start/end page indices to each markdown node using a line->page mapper."""
    for node in node_list:
        start_line = node.get('start_line')
        end_line = node.get('end_line')
        start_page = line_to_page(start_line)
        end_page = line_to_page(end_line)
        if isinstance(start_page, int):
            node['start_index'] = start_page
        if isinstance(end_page, int):
            node['end_index'] = end_page if isinstance(end_page, int) else start_page
        if 'start_index' in node and 'end_index' not in node:
            node['end_index'] = node['start_index']
    return node_list


def update_node_list_with_text_token_count(node_list, model=None):

    def find_all_children(parent_index, parent_level, node_list):
        """Find all direct and indirect children of a parent node"""
        children_indices = []
        
        # Look for children after the parent
        for i in range(parent_index + 1, len(node_list)):
            current_level = node_list[i]['level']
            
            # If we hit a node at same or higher level than parent, stop
            if current_level <= parent_level:
                break
                
            # This is a descendant
            children_indices.append(i)
        
        return children_indices
    
    # Make a copy to avoid modifying the original
    result_list = node_list.copy()
    
    # Process nodes from end to beginning to ensure children are processed before parents
    for i in range(len(result_list) - 1, -1, -1):
        current_node = result_list[i]
        current_level = current_node['level']
        
        # Get all children of this node
        children_indices = find_all_children(i, current_level, result_list)
        
        # Start with the node's own text
        node_text = current_node.get('text', '')
        total_text = node_text
        
        # Add all children's text
        for child_index in children_indices:
            child_text = result_list[child_index].get('text', '')
            if child_text:
                total_text += '\n' + child_text
        
        # Calculate token count for combined text
        result_list[i]['text_token_count'] = count_tokens(total_text, model=model)
    
    return result_list


def tree_thinning_for_index(node_list, min_node_token=None, model=None):
    def find_all_children(parent_index, parent_level, node_list):
        children_indices = []
        
        for i in range(parent_index + 1, len(node_list)):
            current_level = node_list[i]['level']
            
            if current_level <= parent_level:
                break
                
            children_indices.append(i)
        
        return children_indices
    
    result_list = node_list.copy()
    nodes_to_remove = set()
    
    for i in range(len(result_list) - 1, -1, -1):
        if i in nodes_to_remove:
            continue
            
        current_node = result_list[i]
        current_level = current_node['level']
        
        total_tokens = current_node.get('text_token_count', 0)
        
        if total_tokens < min_node_token:
            children_indices = find_all_children(i, current_level, result_list)
            
            children_texts = []
            for child_index in sorted(children_indices):
                if child_index not in nodes_to_remove:
                    child_text = result_list[child_index].get('text', '')
                    if child_text.strip():
                        children_texts.append(child_text)
                    nodes_to_remove.add(child_index)
            
            if children_texts:
                parent_text = current_node.get('text', '')
                merged_text = parent_text
                for child_text in children_texts:
                    if merged_text and not merged_text.endswith('\n'):
                        merged_text += '\n\n'
                    merged_text += child_text
                
                result_list[i]['text'] = merged_text
                
                result_list[i]['text_token_count'] = count_tokens(merged_text, model=model)
    
    for index in sorted(nodes_to_remove, reverse=True):
        result_list.pop(index)
    
    return result_list


def build_tree_from_nodes(node_list):
    if not node_list:
        return []
    
    stack = []
    root_nodes = []
    node_counter = 1
    
    for node in node_list:
        current_level = node['level']
        
        tree_node = {
            'title': node['title'],
            'node_id': str(node_counter).zfill(4),
            'text': node['text'],
            'start_index': node.get('start_index'),
            'end_index': node.get('end_index'),
            'nodes': []
        }
        node_counter += 1
        
        while stack and stack[-1][1] >= current_level:
            stack.pop()
        
        if not stack:
            root_nodes.append(tree_node)
        else:
            parent_node, parent_level = stack[-1]
            parent_node['nodes'].append(tree_node)
        
        stack.append((tree_node, current_level))
    
    return root_nodes


def clean_tree_for_output(tree_nodes):
    cleaned_nodes = []
    
    for node in tree_nodes:
        cleaned_node = {
            'title': node['title'],
            'node_id': node['node_id'],
            'text': node['text'],
            'start_index': node.get('start_index'),
            'end_index': node.get('end_index')
        }
        
        if node['nodes']:
            cleaned_node['nodes'] = clean_tree_for_output(node['nodes'])
        
        cleaned_nodes.append(cleaned_node)
    
    return cleaned_nodes


def build_pages_from_markdown(markdown_lines, lines_per_page=120):
    if not markdown_lines:
        return []

    # Prefer explicit page markers if present.
    markers = _parse_page_markers(markdown_lines)
    if markers:
        pages = []
        for idx, (page_number, start_line) in enumerate(markers):
            end_line = markers[idx + 1][1] - 1 if idx + 1 < len(markers) else len(markdown_lines)
            chunk_lines = markdown_lines[start_line - 1:end_line]
            content = _strip_page_markers_from_text('\n'.join(chunk_lines).strip())
            pages.append({
                'page': int(page_number),
                'content': content
            })
        return pages

    lines_per_page = max(1, int(lines_per_page))
    pages = []
    page_number = 1

    for start in range(0, len(markdown_lines), lines_per_page):
        chunk_lines = markdown_lines[start:start + lines_per_page]
        content = '\n'.join(chunk_lines).strip()
        if not content:
            continue
        pages.append({
            'page': page_number,
            'content': content
        })
        page_number += 1

    return pages


async def md_to_tree(md_path, if_thinning=False, min_token_threshold=None, if_add_node_summary='no', summary_token_threshold=None, model=None, if_add_doc_description='no', if_add_node_text='no', if_add_node_id='yes'):
    with open(md_path, 'r', encoding='utf-8') as f:
        markdown_content = f.read()
    line_count = markdown_content.count('\n') + 1

    print(f"Extracting nodes from markdown...")
    node_list, markdown_lines = extract_nodes_from_markdown(markdown_content)

    pages = build_pages_from_markdown(markdown_lines)

    print(f"Extracting text content from nodes...")
    nodes_with_content = extract_node_text_content(node_list, markdown_lines)
    line_to_page = _build_line_to_page_mapper(markdown_lines)
    nodes_with_content = attach_page_indices(nodes_with_content, line_to_page)
    
    if if_thinning:
        nodes_with_content = update_node_list_with_text_token_count(nodes_with_content, model=model)
        print(f"Thinning nodes...")
        nodes_with_content = tree_thinning_for_index(nodes_with_content, min_token_threshold, model=model)
    
    print(f"Building tree from nodes...")
    tree_structure = build_tree_from_nodes(nodes_with_content)

    if if_add_node_id == 'yes':
        write_node_id(tree_structure)

    print(f"Formatting tree structure...")
    
    if if_add_node_summary == 'yes':
        # Always include text for summary generation
        tree_structure = format_structure(tree_structure, order = ['title', 'node_id', 'start_index', 'end_index', 'summary', 'prefix_summary', 'text', 'nodes'])
        
        print(f"Generating summaries for each node...")
        tree_structure = await generate_summaries_for_structure_md(tree_structure, summary_token_threshold=summary_token_threshold, model=model)
        
        if if_add_node_text == 'no':
            # Remove text after summary generation if not requested
            tree_structure = format_structure(tree_structure, order = ['title', 'node_id', 'start_index', 'end_index', 'summary', 'prefix_summary', 'nodes'])
        
        if if_add_doc_description == 'yes':
            print(f"Generating document description...")
            # Create a clean structure without unnecessary fields for description generation
            clean_structure = create_clean_structure_for_description(tree_structure)
            doc_description = generate_doc_description(clean_structure, model=model)
            return {
                'doc_name': os.path.splitext(os.path.basename(md_path))[0],
                'doc_description': doc_description,
                'line_count': line_count,
                'page_count': len(pages),
                'pages': pages,
                'structure': tree_structure,
            }
    else:
        # No summaries needed, format based on text preference
        if if_add_node_text == 'yes':
            tree_structure = format_structure(tree_structure, order = ['title', 'node_id', 'start_index', 'end_index', 'summary', 'prefix_summary', 'text', 'nodes'])
        else:
            tree_structure = format_structure(tree_structure, order = ['title', 'node_id', 'start_index', 'end_index', 'summary', 'prefix_summary', 'nodes'])
    
    return {
        'doc_name': os.path.splitext(os.path.basename(md_path))[0],
        'line_count': line_count,
        'page_count': len(pages),
        'pages': pages,
        'structure': tree_structure,
    }


if __name__ == "__main__":
    raise SystemExit("Use app/document_assistant_agent.py as the main entrypoint in this trimmed repository.")
