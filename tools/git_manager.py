"""Simple git manager utilities for demo.

These stubs use GitPython if available; otherwise they fall back to subprocess.
"""

import logging
import os
import re
import subprocess
import tempfile
from typing import Dict, Any

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
    # Strip leading fence
    if lines and lines[0].strip().startswith('```'):
        lines = lines[1:]
    # Strip trailing fence
    if lines and lines[-1].strip() == '```':
        lines = lines[:-1]
    # Strip any internal fence lines (LLM sometimes returns multiple blocks)
    lines = [ln for ln in lines if ln.strip() != '```']
    return '\n'.join(lines)


def apply_patch(repo_path: str, patch_text: str) -> Dict[str, Any]:
    """Apply a unified-diff or JSON patch to files under repo_path.

    Supports unified diff only (tries 'patch' utility, falls back to parser).

    Returns {applied: bool, files: [...], errors: [...]}.
    """
    import logging
    logger = logging.getLogger(__name__)

    result = {"applied": False, "files": [], "errors": []}
    try:
        patch_text = _strip_markdown_fences(patch_text)

        if not patch_text or patch_text.strip().startswith('{'):
            result['errors'].append('Only unified diff patches are allowed; JSON patched_content is rejected')
            return result

        if '--- ' not in patch_text or '+++ ' not in patch_text or '@@' not in patch_text:
            result['errors'].append('Patch must be unified diff')
            return result

        applied_files = _apply_unified_diff(repo_path, patch_text, logger)
        result['applied'] = len(applied_files) > 0
        result['files'] = applied_files
        if not result['applied']:
            result['errors'].append('No files were patched (patch command failed or no matching files)')
    except Exception as e:
        logger.exception('Error applying patch')
        result['errors'].append(str(e))
    return result


def _extract_patch_paths(patch_text):
    """Extract repo-relative file paths from +++ headers in a unified diff.

    After `patch -p1`, the first path component is stripped. If the resulting
    path doesn't exist (e.g. LLM added an extra directory prefix), try
    stripping additional components until a valid path is found.
    """
    paths = []
    for line in patch_text.split('\n'):
        if not line.startswith('+++'):
            continue
        parts = line.split('\t')[0].split(' ')
        if len(parts) <= 1:
            continue
        raw = parts[-1]
        # Strip 'b/' prefix (diff convention)
        if raw.startswith('b/'):
            raw = raw[2:]
        # After -p1, this is the path. Normalize separators.
        raw = raw.replace('\\', '/')
        if raw not in paths:
            paths.append(raw)
    return paths


def _apply_unified_diff(repo_path, patch_text, logger):
    """Apply unified-diff. Try 'git apply' first, then 'patch' CLI, fall back to internal parser."""
    applied_files = []

    # Strategy 1: Try git apply (more strict but better error messages)
    try:
        res = subprocess.run(['git', 'apply', '--check', '--verbose'],
                             cwd=repo_path, input=patch_text,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True, timeout=30)
        if res.returncode == 0:
            # Apply for real
            res = subprocess.run(['git', 'apply', '--verbose'],
                                 cwd=repo_path, input=patch_text,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 universal_newlines=True, timeout=30)
            if res.returncode == 0:
                for fpath in _extract_patch_paths(patch_text):
                    corrected = _correct_patch_path(repo_path, fpath)
                    if corrected not in applied_files:
                        applied_files.append(corrected)
                logger.info("Applied unified-diff via 'git apply' to %d files", len(applied_files))
                return applied_files
        else:
            logger.info("git apply --check failed: %s", (res.stderr or '').strip()[:200])
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.info("git apply not available: %s", e)

    # Strategy 2: Try patch CLI with fuzz factor
    patch_stderr = ''
    try:
        res = subprocess.run(['patch', '-p1', '--batch', '--fuzz=3'],
                             cwd=repo_path, input=patch_text, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
        patch_stderr = (res.stderr or '').strip()
        if res.returncode in (0, 1):
            for fpath in _extract_patch_paths(patch_text):
                corrected = _correct_patch_path(repo_path, fpath)
                if corrected not in applied_files:
                    applied_files.append(corrected)
            logger.info("Applied unified-diff via 'patch' to %d files", len(applied_files))
            return applied_files
        else:
            logger.warning("patch -p1 failed (exit %d): %s", res.returncode, patch_stderr[:300])
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.warning("patch -p1 exception: %s", e)

    # Strategy 3: Internal fallback parser
    applied_files = _apply_diff_fallback(repo_path, patch_text, logger)
    return applied_files


def _correct_patch_path(repo_path, fpath):
    """Try progressively stripping directory levels to find the real repo path.

    Handles cases where the LLM generated an extra directory prefix
    (e.g. 'mall-service/src/Main.java' when the real path is 'src/Main.java').
    """
    parts = fpath.replace('\\', '/').split('/')
    # Try the path as-is first
    for i in range(len(parts)):
        candidate = '/'.join(parts[i:])
        if os.path.isfile(os.path.join(repo_path, candidate)):
            return candidate
        # Stop stripping once we hit a common source root
        if parts[i] in ('src', 'main', 'test', 'java', 'resources', 'pom.xml', 'build.gradle'):
            return candidate
    return fpath


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
                raw_path = parts[-1]
                if raw_path.startswith('b/'):
                    raw_path = raw_path[2:]
                current_file = _correct_patch_path(repo_path, raw_path)
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
        abs_path = os.path.realpath(os.path.join(repo_path, file_path))
        repo_root_real = os.path.realpath(repo_path)
        try:
            if os.path.commonpath([repo_root_real, abs_path]) != repo_root_real:
                logger.warning("Rejected path outside repository: %s", abs_path)
                continue
        except ValueError:
            logger.warning("Rejected path outside repository: %s", abs_path)
            continue
        if not os.path.exists(abs_path):
            # New file creation: collect all added lines from hunks
            new_lines = []
            for hunk in hunks:
                for hunk_line in hunk.get('lines', []):
                    if hunk_line.startswith('+') and not hunk_line.startswith('+++'):
                        new_lines.append(hunk_line[1:])
                    elif hunk_line.startswith(' '):
                        new_lines.append(hunk_line[1:])
            if new_lines:
                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                with open(abs_path, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(new_lines) + '\n')
                applied_files.append(file_path)
                logger.info("Created new file: %s (%d lines)", file_path, len(new_lines))
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
            d = os.path.dirname(abs_path)
            if d and not os.path.exists(d):
                os.makedirs(d, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix='.autofix-', dir=d or repo_path, text=True)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    f.write(content)
                os.replace(tmp_path, abs_path)
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
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
    """Revert specified files to a given git ref via git checkout.

    For files that don't exist at the target ref (newly created files),
    deletes them instead.
    """
    if not files:
        return True
    try:
        rel_files = []
        new_files = []
        for f in files:
            if os.path.isabs(f):
                rel = os.path.relpath(f, repo_path)
            else:
                rel = f
            # Check if file exists at target ref
            ret = subprocess.run(
                ['git', '-C', repo_path, 'cat-file', '-e', '%s:%s' % (ref, rel)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            if ret.returncode == 0:
                rel_files.append(rel)
            else:
                new_files.append(rel)

        # Revert files that exist at target ref
        if rel_files:
            subprocess.check_call(
                ['git', '-C', repo_path, 'checkout', ref, '--'] + rel_files,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )

        # Delete files that are newly created (don't exist at target ref)
        for f in new_files:
            abs_f = os.path.join(repo_path, f)
            if os.path.exists(abs_f):
                os.remove(abs_f)
                _git_logger.info("Deleted new file: %s", f)

        _git_logger.info("Reverted %d files, deleted %d new files to %s",
                         len(rel_files), len(new_files), ref)
        return True
    except subprocess.CalledProcessError as e:
        _git_logger.warning("Failed to revert files to %s: %s", ref, e)
        return False
