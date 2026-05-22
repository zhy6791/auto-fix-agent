"""Patch validation utilities for Java file patches."""

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


def _strip_markdown_fences(text):
    lines = text.splitlines()
    if lines and lines[0].strip().startswith('```'):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith('```'):
        lines = lines[:-1]
    # Strip any internal fence lines (LLM sometimes returns multiple blocks)
    lines = [ln for ln in lines if ln.strip() != '```']
    return '\n'.join(lines)


def _is_unified_diff(patch_text):
    text = _strip_markdown_fences(patch_text or '')
    lines = [ln.rstrip('\r') for ln in text.splitlines()]
    has_file_header = False
    has_hunk = False
    for ln in lines:
        if ln.startswith('--- ') or ln.startswith('+++ '):
            has_file_header = True
        elif ln.startswith('@@ '):
            has_hunk = True
    return bool(text.strip()) and has_file_header and has_hunk


def _parse_unified_diff(patch_text):
    """Parse unified diff into file-level structures.

    Returns a list of dicts: {old_path, new_path, hunks, removed_lines, added_lines}
    """
    text = _strip_markdown_fences(patch_text or '')
    files = []
    current = None
    current_hunk = None

    def finish_hunk():
        nonlocal current_hunk
        if current is not None and current_hunk is not None:
            current['hunks'].append(current_hunk)
        current_hunk = None

    def finish_file():
        nonlocal current
        if current is not None:
            finish_hunk()
            files.append(current)
        current = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip('\n')
        if line.startswith('diff --git '):
            finish_file()
            current = {'old_path': None, 'new_path': None, 'hunks': [], 'removed_lines': [], 'added_lines': []}
            continue
        if line.startswith('--- '):
            if current is None:
                current = {'old_path': None, 'new_path': None, 'hunks': [], 'removed_lines': [], 'added_lines': []}
            current['old_path'] = line[4:].strip()  # type: ignore[assignment]
            continue
        if line.startswith('+++ '):
            if current is None:
                current = {'old_path': None, 'new_path': None, 'hunks': [], 'removed_lines': [], 'added_lines': []}
            current['new_path'] = line[4:].strip()  # type: ignore[assignment]
            continue
        if line.startswith('@@ '):
            if current is None:
                continue
            finish_hunk()
            m = re.match(r'^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@', line)
            if not m:
                current_hunk = None
                continue
            current_hunk = {
                'old_start': int(m.group(1)),
                'old_count': int(m.group(2)) if m.group(2) else 1,
                'new_start': int(m.group(3)),
                'new_count': int(m.group(4)) if m.group(4) else 1,
                'lines': [],
            }
            continue
        if current_hunk is not None:
            current_hunk['lines'].append(line)
            if line.startswith('-') and not line.startswith('---'):
                current['removed_lines'].append(line[1:])
            elif line.startswith('+') and not line.startswith('+++'):
                current['added_lines'].append(line[1:])

    finish_file()
    return files


def _normalize_comment_line(line):
    return re.sub(r'\s+', ' ', (line or '').strip()).lower()


def _extract_protected_comment_lines(text):
    """Return normalized comment lines that should not disappear from patches."""
    lines = (text or '').splitlines()
    protected = set()

    # 1) Leading file header comment block
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx < len(lines) and lines[idx].lstrip().startswith(('//', '/*', '/**')):
        while idx < len(lines):
            s = lines[idx].strip()
            if not s:
                protected.add(_normalize_comment_line(lines[idx]))
                idx += 1
                continue
            if s.startswith(('//', '/*', '*', '*/')):
                protected.add(_normalize_comment_line(lines[idx]))
                if '*/' in s:
                    idx += 1
                    break
                idx += 1
                continue
            break

    # 2) Any Javadoc / block comment content and key keyword comments anywhere in file
    in_block = False
    for line in lines:
        s = line.strip()
        lower = s.lower()
        if s.startswith('/**'):
            in_block = True
        if in_block:
            protected.add(_normalize_comment_line(line))
            if '*/' in s:
                in_block = False
            continue
        if s.startswith('/*'):
            in_block = True
            protected.add(_normalize_comment_line(line))
            if '*/' in s:
                in_block = False
            continue
        if s.startswith('//') and any(token in lower for token in ('copyright', 'license', 'todo', 'fixme')):
            protected.add(_normalize_comment_line(line))

    return protected


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
    max_patch_hunks = int(config.get('max_patch_hunks', 3))
    max_hunk_lines = int(config.get('max_hunk_lines', 24))
    max_hunk_span = int(config.get('max_hunk_span', 40))
    max_file_change_ratio = float(config.get('max_file_change_ratio', 0.35))
    repo_root = os.path.abspath(repo_path)
    repo_root_real = os.path.realpath(repo_root)
    result = {'valid': False, 'errors': [], 'changed_lines': 0, 'files': []}

    try:
        patch_text = _strip_markdown_fences(patch_text or '')

        if not patch_text or is_no_safe_patch(patch_text):
            result['errors'].append('LLM did not produce a safe patch')
            return result

        if patch_text.lstrip().startswith('{'):
            result['errors'].append('Only unified diff patches are allowed; JSON patched_content is rejected')
            return result

        if not _is_unified_diff(patch_text):
            result['errors'].append('Patch must be unified diff (--- / +++ / @@ / +/- lines)')
            return result

        files = _parse_unified_diff(patch_text)
        if not files:
            result['errors'].append('Patch does not contain any unified diff file hunks')
            return result

        for item in files:
            old_path = item.get('old_path')
            new_path = item.get('new_path')
            rel_path = new_path if new_path and new_path != '/dev/null' else old_path
            if not rel_path:
                result['errors'].append('Patch file header missing path')
                continue

            rel_path = rel_path[2:] if rel_path.startswith(('a/', 'b/')) else rel_path
            rel_path = os.path.normpath(str(rel_path))
            abs_path = os.path.realpath(os.path.join(repo_root_real, rel_path))
            try:
                if os.path.commonpath([repo_root_real, abs_path]) != repo_root_real:
                    result['errors'].append('Patch path escapes repository: %s' % rel_path)
                    continue
            except ValueError:
                result['errors'].append('Patch path escapes repository: %s' % rel_path)
                continue

            result['files'].append(rel_path)

            old_text = ''
            if os.path.exists(abs_path):
                old_text = file_io.read_file(abs_path)

            changed_lines = len(item.get('removed_lines', [])) + len(item.get('added_lines', []))
            result['changed_lines'] += changed_lines

            is_test_file = 'src/test/java' in str(rel_path).replace('\\', '/')

            # Test files are inherently larger (new test class), skip hunk size limits
            hunks = item.get('hunks', [])
            if not is_test_file:
                if len(hunks) > max_patch_hunks:
                    result['errors'].append('Patch for %s has too many hunks: %d > %d' % (rel_path, len(hunks), max_patch_hunks))

                for hunk in hunks:
                    hunk_changed = sum(1 for ln in hunk.get('lines', []) if ln.startswith(('+', '-')) and not ln.startswith(('+++', '---')))
                    hunk_span = max(int(hunk.get('old_count', 0)), int(hunk.get('new_count', 0)))
                    if hunk_changed > max_hunk_lines:
                        result['errors'].append('Hunk in %s changes too many lines: %d > %d' % (rel_path, hunk_changed, max_hunk_lines))
                    if hunk_span > max_hunk_span:
                        result['errors'].append('Hunk in %s spans too many lines: %d > %d' % (rel_path, hunk_span, max_hunk_span))

            if old_text:
                old_non_empty = len([l for l in old_text.splitlines() if l.strip()])
                if old_non_empty > 0 and changed_lines > old_non_empty * max_file_change_ratio:
                    result['errors'].append('Patch changes too much of %s: %d changed lines over %d non-empty lines' % (rel_path, changed_lines, old_non_empty))

                if source_info:
                    target_rel = os.path.normpath(str(source_info.get('repo_relative_path', '')))
                    item_rel = os.path.normpath(str(rel_path))
                    if target_rel and item_rel != target_rel and not is_test_file:
                        result['errors'].append('Patch touches files outside analyzed target: %s' % rel_path)

                protected_lines = _extract_protected_comment_lines(old_text)
                if protected_lines:
                    removed_lines = {_normalize_comment_line(ln) for ln in item.get('removed_lines', [])}
                    added_lines = {_normalize_comment_line(ln) for ln in item.get('added_lines', [])}
                    for protected_line in protected_lines:
                        if not protected_line:
                            continue
                        if protected_line in removed_lines and protected_line not in added_lines:
                            result['errors'].append('Protected comment line was removed from %s: %s' % (rel_path, protected_line))
                            break

                # Java structure validation: only for source files, not test files
                if source_info and old_text and not is_test_file:
                    line_no = source_info.get('line_no')
                    new_text_guess = old_text
                    for hunk in reversed(hunks):
                        old_start = int(hunk.get('old_start', 1)) - 1
                        old_count = int(hunk.get('old_count', 0))
                        replacement = []
                        for hunk_line in hunk.get('lines', []):
                            if hunk_line.startswith('+') and not hunk_line.startswith('+++'):
                                replacement.append(hunk_line[1:])
                            elif hunk_line.startswith('-') and not hunk_line.startswith('---'):
                                continue
                            elif hunk_line.startswith(' '):
                                replacement.append(hunk_line[1:])
                            else:
                                replacement.append(hunk_line)
                        new_lines = new_text_guess.splitlines()
                        new_lines[old_start:old_start + old_count] = replacement
                        new_text_guess = '\n'.join(new_lines)

                    struct_errors = validate_java_structure(old_text, new_text_guess, int(line_no) if line_no else None)
                    if struct_errors:
                        result['errors'].extend(struct_errors)


        # Only enforce max_patch_lines on non-test files (test files are inherently larger)
        non_test_changed = 0
        for item in files:
            new_path = item.get('new_path') or item.get('old_path') or ''
            rel = new_path[2:] if new_path.startswith(('a/', 'b/')) else new_path
            if 'src/test/java' not in str(rel).replace('\\', '/'):
                non_test_changed += len(item.get('removed_lines', [])) + len(item.get('added_lines', []))
        if non_test_changed > max_patch_lines:
            result['errors'].append('Patch too large: %s > %s' % (non_test_changed, max_patch_lines))

        result['valid'] = len(result['errors']) == 0
        return result
    except Exception as e:
        result['errors'].append(str(e))
        return result
