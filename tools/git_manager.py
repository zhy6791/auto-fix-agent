"""Simple git manager utilities for demo.

These stubs use GitPython if available; otherwise they fall back to subprocess.
"""

import logging
import os
import re
import subprocess
from typing import Dict, Any, Optional, Tuple

_git_logger = logging.getLogger(__name__)


def detect_repo_root(path: str) -> str:
    """Return absolute path to repo root (walk up until .git found) or raise FileNotFoundError."""
    p = os.path.abspath(path)
    while True:
        if os.path.isdir(os.path.join(p, '.git')):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            raise FileNotFoundError(f".git not found in path ancestry of {path}")
        p = parent


def get_current_branch(repo_path: str) -> str:
    """Return the current branch name, or empty string on failure."""
    try:
        out = subprocess.check_output(
            ["git", "-C", repo_path, "rev-parse", "--abbrev-ref", "HEAD"],
            universal_newlines=True
        )
        return out.strip()
    except subprocess.CalledProcessError:
        return ''


def create_branch(repo_path: str, branch_name: str) -> bool:
    """Create and checkout a local branch. Returns True on success."""
    try:
        subprocess.check_call(["git", "-C", repo_path, "checkout", "-b", branch_name])
        return True
    except subprocess.CalledProcessError:
        return False


def commit_changes(repo_path: str, message: str, files: list = None) -> bool:
    """Stage and commit changes on the current branch. Returns True on success."""
    try:
        if files:
            subprocess.check_call(["git", "-C", repo_path, "add"] + files)
        else:
            # No files specified — only stage tracked-file changes, not untracked
            subprocess.check_call(["git", "-C", repo_path, "add", "-u"])
        subprocess.check_call(["git", "-C", repo_path, "commit", "-m", message])
        return True
    except subprocess.CalledProcessError:
        return False


def checkout_branch(repo_path: str, branch_name: str) -> bool:
    """Checkout an existing branch. Returns True on success."""
    try:
        subprocess.check_call(["git", "-C", repo_path, "checkout", branch_name])
        return True
    except subprocess.CalledProcessError:
        return False


def _strip_markdown_fences(text):
    """Strip markdown code fences from patch text if present."""
    lines = text.split('\n')
    if lines and lines[0].strip().startswith('```'):
        lines = lines[1:]
    if lines and lines[-1].strip() == '```':
        lines = lines[:-1]
    return '\n'.join(lines)


def apply_patch(repo_path: str, patch_text: str) -> Dict[str, Any]:
    """Apply a unified-diff or JSON patch to files under repo_path.

    Supports:
    1. JSON format: {"files": [{"path": "...", "patched_content": "..."}]}
    2. Unified diff format (tries 'patch' utility, falls back to parser)

    Returns {applied: bool, files: [...], errors: [...]}.
    """
    import logging
    logger = logging.getLogger(__name__)

    result = {"applied": False, "files": [], "errors": []}
    try:
        patch_text = _strip_markdown_fences(patch_text)

        if patch_text.strip().startswith('{'):
            # JSON mapping format
            import json
            obj = json.loads(patch_text)
            files = obj.get('files', [])
            for ent in files:
                rel = ent['path']
                content = ent['patched_content']
                abs_path = os.path.normpath(os.path.join(repo_path, str(rel)))
                # Path traversal protection: ensure resolved path stays within repo
                if not abs_path.startswith(os.path.normpath(repo_path) + os.sep) and abs_path != os.path.normpath(repo_path):
                    result['errors'].append('Path traversal rejected: %s' % rel)
                    continue
                d = os.path.dirname(abs_path)
                if d and not os.path.exists(d):
                    os.makedirs(d, exist_ok=True)
                with open(abs_path, 'w', encoding='utf-8') as f:
                    f.write(content)
                result['files'].append(rel)
            result['applied'] = True
            logger.info(f"Applied JSON patch to {len(result['files'])} files")
        else:
            applied_files = _apply_unified_diff(repo_path, patch_text, logger)
            result['applied'] = len(applied_files) > 0
            result['files'] = applied_files
            if not result['applied']:
                result['errors'].append('Failed to apply unified-diff patch')
    except Exception as e:
        logger.exception('Error applying patch')
        result['errors'].append(str(e))
    return result


def _apply_unified_diff(repo_path, patch_text, logger):
    """Apply unified-diff. Try 'patch' CLI first, fall back to internal parser."""
    applied_files = []
    try:
        res = subprocess.run(['patch', '-p1', '--batch'], cwd=repo_path,
                             input=patch_text, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
        if res.returncode in (0, 1):
            for line in patch_text.split('\n'):
                if line.startswith('+++'):
                    parts = line.split('\t')[0].split(' ')
                    if len(parts) > 1:
                        fpath = parts[-1].lstrip('b/')
                        if fpath not in applied_files:
                            applied_files.append(fpath)
            logger.info(f"Applied unified-diff via 'patch' to {len(applied_files)} files")
            return applied_files
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    applied_files = _apply_diff_fallback(repo_path, patch_text, logger)
    return applied_files


_HUNK_HEADER_RE = re.compile(r'^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@')


def _apply_diff_fallback(repo_path, patch_text, logger):
    """Fallback: parse unified diff and apply patches against original files.

    Reads each original file, applies hunks in reverse order to preserve
    line number references, then writes the full patched content back.
    """
    applied_files = []

    # Parse diff: file_path -> list of hunks
    files = {}  # file_path -> list of hunk dicts
    current_file = None
    current_hunk = None

    for line in patch_text.split('\n'):
        if line.startswith('+++'):
            parts = line.split('\t')[0].split(' ')
            if len(parts) > 1:
                current_file = parts[-1].lstrip('b/')
                files[current_file] = []
                current_hunk = None
        elif line.startswith('@@'):
            m = _HUNK_HEADER_RE.match(line)
            if m and current_file:
                current_hunk = {
                    'old_start': int(m.group(1)),
                    'old_count': int(m.group(2)) if m.group(2) else 0,
                    'new_start': int(m.group(3)),
                    'new_count': int(m.group(4)) if m.group(4) else 0,
                    'lines': []
                }
                files[current_file].append(current_hunk)
        elif current_hunk is not None:
            if line.startswith('\\'):
                # '\ No newline at end of file' — skip
                continue
            current_hunk['lines'].append(line)

    # Apply hunks per file
    for file_path, hunks in files.items():
        abs_path = os.path.join(repo_path, file_path)
        if not os.path.exists(abs_path):
            logger.warning("File not found for patching: %s", abs_path)
            continue

        try:
            with open(abs_path, 'r', encoding='utf-8') as f:
                original_lines = f.read().splitlines()

            # Apply hunks in reverse order to keep line numbers valid
            for hunk in reversed(hunks):
                old_start = hunk['old_start'] - 1  # 0-indexed
                old_count = hunk['old_count']

                new_hunk_lines = []
                for hunk_line in hunk['lines']:
                    if hunk_line.startswith('+'):
                        new_hunk_lines.append(hunk_line[1:])
                    elif hunk_line.startswith('-'):
                        pass  # removed line
                    elif hunk_line.startswith(' '):
                        # Standard diff: context line has leading space
                        new_hunk_lines.append(hunk_line[1:])
                    else:
                        # LLM-generated diffs may omit the leading space
                        new_hunk_lines.append(hunk_line)

                original_lines[old_start:old_start + old_count] = new_hunk_lines

            content = '\n'.join(original_lines)
            with open(abs_path, 'w', encoding='utf-8') as f:
                f.write(content)
            logger.info("Wrote patched file: %s", file_path)
            applied_files.append(file_path)
        except Exception as e:
            logger.warning("Failed to patch file %s: %s", file_path, e)

    return applied_files


def push_branch(repo_path, branch_name, remote='origin'):
    """Push branch to remote. Returns True on success."""
    try:
        subprocess.check_call(
            ["git", "-C", repo_path, "push", "-u", remote, branch_name],
            timeout=60
        )
        return True
    except subprocess.CalledProcessError as e:
        _git_logger.error("Failed to push branch %s: %s", branch_name, e)
        return False


def get_remote_url(repo_path, remote='origin'):
    """Get the remote URL. Returns empty string on failure."""
    try:
        out = subprocess.check_output(
            ["git", "-C", repo_path, "remote", "get-url", remote],
            universal_newlines=True
        )
        return out.strip()
    except subprocess.CalledProcessError:
        return ''


def parse_gitee_owner_repo(remote_url):
    """Parse owner and repo name from Gitee remote URL.

    Handles formats:
      - https://gitee.com/owner/repo.git
      - git@gitee.com:owner/repo.git
    Returns (owner, repo) or (None, None).
    """
    # HTTPS format
    m = re.match(r'https?://gitee\.com/([^/]+)/([^/]+?)(?:\.git)?$', remote_url)
    if m:
        return m.group(1), m.group(2)
    # SSH format
    m = re.match(r'git@gitee\.com:([^/]+)/([^/]+?)(?:\.git)?$', remote_url)
    if m:
        return m.group(1), m.group(2)
    return None, None


def revert_files(repo_path: str, files: list, ref: str = 'HEAD') -> bool:
    """Revert specified files to a given git ref via git checkout."""
    if not files:
        return True
    try:
        rel_files = []
        for f in files:
            if os.path.isabs(f):
                rel_files.append(os.path.relpath(f, repo_path))
            else:
                rel_files.append(f)
        subprocess.check_call(
            ['git', '-C', repo_path, 'checkout', ref, '--'] + rel_files,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        _git_logger.info("Reverted %d files to %s", len(rel_files), ref)
        return True
    except subprocess.CalledProcessError as e:
        _git_logger.warning("Failed to revert files to %s: %s", ref, e)
        return False
