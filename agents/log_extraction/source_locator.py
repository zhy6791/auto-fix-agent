"""Source file location utilities for Java stack frames."""

import os
import re


def find_source_location(repo_path, frame, file_io):
    """Find source file and return context information for the top stack frame."""
    class_name = frame.get('class_name', '')
    line_no = frame.get('line_no')
    repo_root = os.path.abspath(repo_path)

    rel_candidate = class_name.replace('.', os.sep) + '.java'
    candidates = [
        os.path.join(repo_root, 'src', 'main', 'java', rel_candidate),
        os.path.join(repo_root, 'src', 'test', 'java', rel_candidate),
        os.path.join(repo_root, rel_candidate),
    ]

    source_path = None
    for candidate in candidates:
        if os.path.exists(candidate):
            source_path = candidate
            break

    if source_path is None:
        # Fallback: search by class basename
        simple_name = class_name.split('.')[-1] + '.java'
        for root, _, files in os.walk(repo_root):
            if simple_name in files:
                source_path = os.path.join(root, simple_name)
                break

    if source_path is None:
        raise FileNotFoundError('Could not locate source file for class: %s' % class_name)

    source_text = file_io.read_file(str(source_path))
    source_lines = source_text.splitlines()
    total_lines = len(source_lines)
    if line_no and line_no > 0:
        start = max(1, line_no - 3)
        end = min(total_lines, line_no + 3)
    else:
        start = 1
        end = min(total_lines, 20)

    snippet_lines = []
    for idx in range(start, end + 1):
        snippet_lines.append(source_lines[idx - 1])

    return {
        'source_path': source_path,
        'repo_relative_path': os.path.relpath(str(source_path), str(repo_root)),
        'class_name': class_name,
        'method': frame.get('method'),
        'line_no': line_no,
        'context_snippet': '\n'.join(snippet_lines),
        'full_source': source_text,
    }


def select_best_frame(repo_path, parsed_stack, find_fn):
    """Select the best (most relevant) frame from the stack.

    Prioritizes frames where source files exist in the repo.
    Prefers application packages over framework packages.
    """
    if not parsed_stack:
        return None

    for frame in parsed_stack:
        try:
            info = find_fn(repo_path, frame)
            return frame
        except (FileNotFoundError, OSError):
            continue

    return None


def locate_file_by_class_or_path(repo_path, class_name, file_path):
    """Locate file by class name or file path."""
    repo_root = os.path.abspath(repo_path)

    # Try file path first
    if file_path:
        candidate = os.path.join(repo_root, file_path.lstrip('/').lstrip('\\'))
        if os.path.exists(candidate):
            return candidate

    # Try class name
    if class_name:
        rel_candidate = class_name.replace('.', os.sep) + '.java'
        candidates = [
            os.path.join(repo_root, 'src', 'main', 'java', rel_candidate),
            os.path.join(repo_root, 'src', 'test', 'java', rel_candidate),
        ]
        for cand in candidates:
            if os.path.exists(cand):
                return cand

    return None


def find_method_line(lines, method_name):
    """Find the line number of a method definition."""
    if not method_name:
        return None

    pattern = re.compile(r'(?:public|private|protected)?\s+\w+\s+' + re.escape(method_name) + r'\s*\(')
    for idx, line in enumerate(lines):
        if pattern.search(line):
            return idx + 1  # 1-based line number

    return None
