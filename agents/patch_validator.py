"""Patch validation utilities for Java file patches."""

import json
import difflib
import os
import re


def is_no_safe_patch(patch_text):
    """Check if the LLM returned NO_SAFE_PATCH."""
    return isinstance(patch_text, str) and 'NO_SAFE_PATCH' in patch_text


def estimate_changed_lines(old_text, new_text):
    """Count differing lines between old and new text."""
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    overlap = min(len(old_lines), len(new_lines))
    changed = 0
    for idx in range(overlap):
        if old_lines[idx] != new_lines[idx]:
            changed += 1
    changed += abs(len(old_lines) - len(new_lines))
    return changed


def validate_java_structure(old_text, new_text, line_no, window=8):
    """Comprehensive validation for Java file patches.

    Ensures:
    1. package declaration is unchanged
    2. import list only grows (no deletions)
    3. all original methods still exist (except those in window)
    4. no massive line deletions outside window

    Returns list of error messages, or [] if valid.
    """
    errors = []

    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()

    # Extract package and imports from both versions
    def _extract_sections(lines):
        pkg = None
        imports = []
        pkg_line = None
        import_end = 0
        in_header = True

        for idx, ln in enumerate(lines):
            s = ln.strip()
            if not s or s.startswith('//') or s.startswith('/*') or s.startswith('*') or s.startswith('*/'):
                continue
            if s.startswith('package '):
                pkg = s
                pkg_line = idx
            elif s.startswith('import '):
                imports.append(s)
                import_end = idx
            elif s.startswith('@'):
                # Annotation — still in header region, skip
                continue
            elif in_header and pkg is not None:
                # First non-import, non-annotation line after package — end of header
                break

        return {'pkg': pkg, 'imports': sorted(imports), 'pkg_line': pkg_line, 'import_end': import_end}

    old_sec = _extract_sections(old_lines)
    new_sec = _extract_sections(new_lines)

    # 1) Package must be identical
    if old_sec['pkg'] != new_sec['pkg']:
        errors.append('Package declaration was modified: "%s" -> "%s"' % (old_sec['pkg'], new_sec['pkg']))

    # 2) Imports must not be deleted or modified (only new imports allowed)
    old_imports_set = set(old_sec['imports'])
    new_imports_set = set(new_sec['imports'])
    deleted_imports = old_imports_set - new_imports_set
    modified_imports = []
    for old_imp in old_sec['imports']:
        for new_imp in new_sec['imports']:
            if old_imp != new_imp and old_imp.split()[1] == new_imp.split()[1]:
                modified_imports.append('%s -> %s' % (old_imp, new_imp))

    if deleted_imports:
        errors.append('Imports were deleted: %s' % ', '.join(deleted_imports))
    if modified_imports:
        errors.append('Imports were modified: %s' % '; '.join(modified_imports))

    # 3) Extract method signatures to ensure none are deleted outside window
    method_sig_re = re.compile(r'^\s*(?:(?:public|protected|private|static|final|abstract|synchronized)\s+)*[\w<>,\s\[\]]+\s+([\w$]+)\s*\(')

    def _extract_methods(lines):
        methods = {}
        for idx, ln in enumerate(lines):
            m = method_sig_re.match(ln)
            if m:
                methods[m.group(1)] = idx + 1  # 1-based line number
        return methods

    old_methods = _extract_methods(old_lines)
    new_methods = _extract_methods(new_lines)

    if line_no:
        lower = max(1, line_no - window)
        upper = line_no + window

        # Check if any methods disappeared outside the window
        deleted_methods = set(old_methods.keys()) - set(new_methods.keys())
        for method_name in deleted_methods:
            method_line = old_methods[method_name]
            if method_line < lower or method_line > upper:
                errors.append('Method "%s" (line %d) was deleted outside the problem window' % (method_name, method_line))

    # 4) Detect massive line deletions outside window
    if line_no and line_no > 0:
        lower = max(1, line_no - window)
        upper = line_no + window

        # Count deletions outside window
        matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines)
        external_deletions = 0
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == 'delete' or tag == 'replace':
                for line_idx in range(i1 + 1, i2 + 1):
                    if line_idx < lower or line_idx > upper:
                        external_deletions += 1

        # If more than 20% of non-header lines deleted outside window, reject
        non_header_lines = len(old_lines) - old_sec['import_end'] - 1
        if non_header_lines > 0 and external_deletions > max(5, non_header_lines * 0.2):
            errors.append('Too many lines deleted outside problem window: %d deletions detected' % external_deletions)

    # 5) Ensure new file is not drastically shorter (missing content)
    old_non_empty = len([l for l in old_lines if l.strip()])
    new_non_empty = len([l for l in new_lines if l.strip()])
    if new_non_empty < old_non_empty * 0.5:
        errors.append('New file has drastically fewer lines (%.0f%% of original size)' % (100 * new_non_empty / old_non_empty if old_non_empty else 0))

    # 6) Reject patches where code includes line-number prefixes (LLM artifact)
    line_no_pattern = re.compile(r'^\s*\d+:\s+')
    code_lines_with_prefix = 0
    for ln in new_lines:
        stripped = ln.strip()
        if stripped and not stripped.startswith('//') and not stripped.startswith('*') and line_no_pattern.match(stripped):
            code_lines_with_prefix += 1

    if code_lines_with_prefix > 0:
        errors.append('CRITICAL: Patched code contains line-number prefixes (LLM copied snippet format). %d lines with prefix detected. This is NOT valid Java code.' % code_lines_with_prefix)

    return errors


def validate_patch(config, repo_path, patch_text, source_info, file_io):
    """Validate patch text before applying."""
    max_patch_lines = int(config.get('max_patch_lines', 40))
    repo_root = os.path.abspath(repo_path)
    result = {'valid': False, 'errors': [], 'changed_lines': 0, 'files': []}

    try:
        if not patch_text or is_no_safe_patch(patch_text):
            result['errors'].append('LLM did not produce a safe patch')
            return result

        if patch_text.lstrip().startswith('{'):
            patch_obj = json.loads(patch_text)
            files = patch_obj.get('files', [])
            for item in files:
                rel_path = item.get('path', '')
                patched_content = item.get('patched_content', '')
                if not rel_path:
                    result['errors'].append('Patch item missing path')
                    continue
                abs_path = os.path.abspath(os.path.join(str(repo_root), str(rel_path)))
                if not abs_path.startswith(repo_root):
                    result['errors'].append('Patch path escapes repository: %s' % rel_path)
                    continue
                result['files'].append(rel_path)

                old_text = ''
                if os.path.exists(abs_path):
                    old_text = file_io.read_file(abs_path)
                result['changed_lines'] += estimate_changed_lines(old_text, patched_content)

                if source_info and old_text:
                    target_rel = os.path.normpath(str(source_info.get('repo_relative_path', '')))
                    item_rel = os.path.normpath(str(rel_path))
                    # Allow test patches in src/test/java/ even if they differ from target
                    is_test_file = 'src/test/java' in str(rel_path).replace('\\', '/')
                    if target_rel and item_rel != target_rel and not is_test_file:
                        result['errors'].append('Patch touches files outside analyzed target: %s' % rel_path)
                    else:
                        line_no = source_info.get('line_no')
                        # Run comprehensive structural checks
                        struct_errors = validate_java_structure(old_text, patched_content, int(line_no) if line_no else None)
                        if struct_errors:
                            result['errors'].extend(struct_errors)

        else:
            # Simple unified diff heuristics
            added = 0
            removed = 0
            for line in patch_text.splitlines():
                if line.startswith('+++') or line.startswith('---') or line.startswith('@@'):
                    continue
                if line.startswith('+'):
                    added += 1
                elif line.startswith('-'):
                    removed += 1
            result['changed_lines'] = max(added, removed)

        if result['changed_lines'] > max_patch_lines:
            result['errors'].append('Patch too large: %s > %s' % (result['changed_lines'], max_patch_lines))

        result['valid'] = len(result['errors']) == 0
        return result
    except Exception as e:
        result['errors'].append(str(e))
        return result
