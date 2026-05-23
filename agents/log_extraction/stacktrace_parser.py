"""Stack trace parsing utilities for Java exception logs."""

import re

# Pattern for stack frame lines to skip when searching for exception start
# Matches: \tat com.Foo.bar(Foo.java:42) with optional ~[jar:version] suffix
FRAME_LINE_RE = re.compile(r'^\s*at\s+[\w.$<>/]+\([^)]*\)(\s+~\[[^\]]*\])?\s*$')


def extract_latest_exception_block(text):
    """Extract the latest contiguous exception block from log text."""
    if not text:
        return ''

    lines = text.splitlines()
    start_idx = -1

    exception_markers = ('Exception', 'Error', 'Throwable')
    for idx in range(len(lines) - 1, -1, -1):
        line = lines[idx].strip()
        if not line:
            continue
        # Skip stack frame lines — class names like ErrorReportValve
        # contain "Error" but are not exception declarations.
        if FRAME_LINE_RE.match(line):
            continue
        if line.startswith('Caused by:') or any(marker in line for marker in exception_markers):
            start_idx = idx
            break

    if start_idx == -1:
        return text.strip()

    collected = []
    blank_count = 0
    for line in lines[start_idx:]:
        if not line.strip():
            blank_count += 1
            if blank_count >= 2:
                break
            collected.append(line)
        else:
            blank_count = 0
            collected.append(line)

    return '\n'.join(collected).strip()


def parse_stacktrace(text):
    """Parse Java-like stack trace into a list of frames.

    Returns list of dicts:
    {exception_type, class_name, method, line_no, source_file, raw_line}
    """
    if not text:
        return []

    frames = []
    exception_type = ''
    exception_re = re.compile(r'(?P<type>[A-Za-z_][\w.$]*(?:Exception|Error))(?:[:\s]|$)')
    # Matches: at com.Foo.bar(Foo.java:42) with optional ~[jar:version] suffix
    frame_re = re.compile(r'^(?:at\s+)?(?P<class>[\w.$<>]+)\.(?P<method>[\w$<>]+)\((?P<source>[^:()]+)(?::(?P<line>\d+))?\)(\s+~\[[^\]]*\])?$')

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith('Caused by:'):
            m = exception_re.search(line)
            if m:
                exception_type = m.group('type')
            continue

        if not exception_type:
            m = exception_re.search(line)
            if m:
                exception_type = m.group('type')

        m = frame_re.match(line)
        if m:
            source_file = m.group('source')
            if source_file == 'Native Method' or source_file == 'Unknown Source':
                continue
            line_no = m.group('line')
            frames.append({
                'exception_type': exception_type,
                'class_name': m.group('class'),
                'method': m.group('method'),
                'line_no': int(line_no) if line_no else None,
                'source_file': source_file,
                'raw_line': raw_line,
            })

    return frames
