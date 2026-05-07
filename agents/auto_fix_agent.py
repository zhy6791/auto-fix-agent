"""AutoFixAgent Phase 3 implementation.

This module provides a single-agent pipeline for:
log reading -> stack trace parsing -> source locating -> LLM patch generation
-> patch validation -> optional local branch creation and patch application.
"""

import json
import logging
import os
import re
from datetime import datetime

from tools import file_io, exec_cmd, git_manager
from integrations.llm_client import LLMClient


logger = logging.getLogger(__name__)


class AutoFixAgent:
    def __init__(self, config, tools=None, llm_client=None):
        self.config = config or {}
        self.tools = tools or {
            'file_io': file_io,
            'exec_cmd': exec_cmd,
            'git_manager': git_manager,
        }
        self.llm_client = llm_client or self._build_llm_client()

    def _build_llm_client(self):
        llm_cfg = self.config.get('llm', {}) or {}
        api_key_ref = llm_cfg.get('api_key_env', '')
        base_url = llm_cfg.get('base_url')
        model = llm_cfg.get('model', 'gpt-4o-mini')
        temperature = llm_cfg.get('temperature', 0.2)
        timeout = int(llm_cfg.get('timeout', 300))

        # Prefer actual environment variable, otherwise fall back to the configured value.
        api_key = os.environ.get(api_key_ref)
        if not api_key:
            api_key = api_key_ref

        return LLMClient(api_key=api_key, model=model, temperature=temperature, base_url=base_url, timeout=timeout)

    def run_pipeline(self, dry_run=True):
        """Run the full Phase 3 pipeline.

        Returns a structured report dict.
        """
        report = {
            'status': 'started',
            'dry_run': dry_run,
            'raw_stack': '',
            'parsed_stack': [],
            'located_files': [],
            'prompt': '',
            'patch_text': '',
            'branch_name': '',
            'apply_result': {'applied': False, 'files': [], 'errors': [], 'dry_run': dry_run},
            'build_result': None,
            'error': None,
        }

        try:
            logs_path = self.config.get('logs_path')
            repo_path = self.config.get('repo_path')
            if not logs_path:
                raise ValueError('logs_path not set in config')
            if not repo_path:
                raise ValueError('repo_path not set in config')

            raw_stack = self._read_latest_stack(logs_path)
            report['raw_stack'] = raw_stack

            parsed_stack = self.parse_stacktrace(raw_stack)
            report['parsed_stack'] = parsed_stack

            if not parsed_stack:
                raise ValueError('No stack trace entries parsed from logs')

            # Find the best frame to analyze: prioritize app code over framework
            best_frame = self._select_best_frame(repo_path, parsed_stack)
            if not best_frame:
                raise ValueError('Could not find analyzable frame in stack trace')
            
            source_info = self.find_source_location(repo_path, best_frame)
            report['located_files'] = [source_info]

            prompt = self.build_prompt(raw_stack, source_info)
            report['prompt'] = prompt

            max_tokens = int(self.config.get('max_tokens', 8192))
            patch_text = self.llm_client.generate_patch(prompt, max_tokens=max_tokens)
            report['patch_text'] = patch_text

            if self._is_no_safe_patch(patch_text):
                raise ValueError(patch_text)

            validation = self.validate_patch(repo_path, patch_text)
            if not validation['valid']:
                raise ValueError('Patch validation failed: %s' % '; '.join(validation['errors']))

            branch_name = self._make_branch_name()
            report['branch_name'] = branch_name

            if not dry_run:
                branch_ok = self.tools['git_manager'].create_branch(repo_path, branch_name)
                if not branch_ok:
                    raise RuntimeError('Failed to create branch: %s' % branch_name)

                apply_result = self.tools['git_manager'].apply_patch(repo_path, patch_text)
                apply_result['dry_run'] = False
                report['apply_result'] = apply_result

                if self.config.get('run_tests_on_apply') and apply_result.get('applied'):
                    report['build_result'] = self._run_build(repo_path)
            else:
                report['apply_result'] = {
                    'applied': False,
                    'files': [],
                    'errors': [],
                    'dry_run': True,
                    'message': 'Dry run mode: branch creation and patch application skipped',
                }

            report['status'] = 'completed'
            return report

        except Exception as e:
            logger.exception('AutoFixAgent pipeline failed')
            report['status'] = 'failed'
            report['error'] = str(e)
            return report

    def _read_latest_stack(self, logs_path):
        _, chunk = self.tools['file_io'].tail_file(logs_path)
        return self.extract_latest_exception_block(chunk)

    # Pattern for stack frame lines to skip when searching for exception start
    _FRAME_LINE_RE = re.compile(r'^\s*at\s+[\w.$<>/]+\([^)]*\)\s*$')

    def extract_latest_exception_block(self, text):
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
            if self._FRAME_LINE_RE.match(line):
                continue
            if line.startswith('Caused by:') or any(marker in line for marker in exception_markers):
                start_idx = idx
                break

        if start_idx == -1:
            return text.strip()

        collected = []
        for line in lines[start_idx:]:
            if collected and not line.strip():
                break
            collected.append(line)

        return '\n'.join(collected).strip()

    def _select_best_frame(self, repo_path, parsed_stack):
        """Select the best (most relevant) frame from the stack.
        
        Prioritizes frames where source files exist in the repo.
        Prefers application packages over framework packages.
        """
        if not parsed_stack:
            return None
        
        for frame in parsed_stack:
            try:
                info = self.find_source_location(repo_path, frame)
                return frame
            except (FileNotFoundError, OSError):
                continue
        
        return None

    def parse_stacktrace(self, text):
        """Parse Java-like stack trace into a list of frames.

        Returns list of dicts:
        {exception_type, class_name, method, line_no, source_file, raw_line}
        """
        if not text:
            return []

        frames = []
        exception_type = ''
        exception_re = re.compile(r'(?P<type>[A-Za-z_][\w.$]*(?:Exception|Error))(?:[:\s]|$)')
        frame_re = re.compile(r'^(?:at\s+)?(?P<class>[\w.$<>]+)\.(?P<method>[\w$<>]+)\((?P<source>[^:()]+)(?::(?P<line>\d+))?\)$')

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

    def find_source_location(self, repo_path, frame):
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

        source_text = self.tools['file_io'].read_file(str(source_path))
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
            snippet_lines.append('%d: %s' % (idx, source_lines[idx - 1]))

        return {
            'source_path': source_path,
            'repo_relative_path': os.path.relpath(str(source_path), str(repo_root)),
            'class_name': class_name,
            'method': frame.get('method'),
            'line_no': line_no,
            'context_snippet': '\n'.join(snippet_lines),
        }

    def build_prompt(self, raw_stack, source_info):
        """Build the LLM prompt for minimal patch generation."""
        max_patch_lines = self.config.get('max_patch_lines', 40)
        prompt = []
        prompt.append('## 任务')
        prompt.append('根据以下 Java 异常堆栈和源码，生成一个最小化补丁以修复该异常。')
        prompt.append('')
        prompt.append('## 约束')
        prompt.append('- 修改最少（最多 %d 行）' % max_patch_lines)
        prompt.append('- 保证代码编译通过')
        prompt.append('- 不修改方法签名或删除业务逻辑')
        prompt.append('- 优先使用 null check、边界检查等防御性编程')
        prompt.append('')
        prompt.append('## 异常信息')
        prompt.append('```')
        prompt.append(raw_stack[:500])  # Limit stack size
        prompt.append('```')
        prompt.append('')
        prompt.append('## 源码位置与上下文')
        prompt.append('文件: %s (repo 相对路径)' % source_info.get('repo_relative_path'))
        prompt.append('问题行: %s' % source_info.get('line_no'))
        prompt.append('方法: %s' % source_info.get('method'))
        prompt.append('')
        prompt.append('```java')
        prompt.append(source_info.get('context_snippet', ''))
        prompt.append('```')
        prompt.append('')
        prompt.append('## 输出格式')
        prompt.append('返回以下两种格式之一:')
        prompt.append('')
        prompt.append('### 选项 1: JSON 格式（推荐用于精确修改）')
        prompt.append('```json')
        prompt.append('{')
        prompt.append('  "files": [')
        prompt.append('    {')
        prompt.append('      "path": "src/main/java/...",')
        prompt.append('      "patched_content": "完整的修复后文件内容"')
        prompt.append('    }')
        prompt.append('  ]')
        prompt.append('}')
        prompt.append('```')
        prompt.append('')
        prompt.append('### 选项 2: Unified Diff 格式')
        prompt.append('```')
        prompt.append('--- a/src/main/java/...')
        prompt.append('+++ b/src/main/java/...')
        prompt.append('@@  ... @@')
        prompt.append('修改内容')
        prompt.append('```')
        prompt.append('')
        prompt.append('### 选项 3: 无安全补丁')
        prompt.append('如果无法生成安全补丁，返回: NO_SAFE_PATCH: 原因说明')
        return '\n'.join(prompt)

    def validate_patch(self, repo_path, patch_text):
        """Validate patch text before applying."""
        max_patch_lines = int(self.config.get('max_patch_lines', 40))
        repo_root = os.path.abspath(repo_path)
        result = {'valid': False, 'errors': [], 'changed_lines': 0, 'files': []}

        try:
            if not patch_text or self._is_no_safe_patch(patch_text):
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
                        old_text = self.tools['file_io'].read_file(abs_path)
                    result['changed_lines'] += self._estimate_changed_lines(old_text, patched_content)

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

    def _estimate_changed_lines(self, old_text, new_text):
        old_lines = old_text.splitlines()
        new_lines = new_text.splitlines()
        overlap = min(len(old_lines), len(new_lines))
        changed = 0
        for idx in range(overlap):
            if old_lines[idx] != new_lines[idx]:
                changed += 1
        changed += abs(len(old_lines) - len(new_lines))
        return changed

    def _make_branch_name(self):
        prefix = self.config.get('branch_prefix', 'fix/')
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        return '%sauto-%s' % (prefix, timestamp)

    def _run_build(self, repo_path):
        build_tool = self.config.get('java_build', 'maven')
        if build_tool == 'maven':
            cmd = ['mvn', 'test']
        else:
            cmd = ['gradle', 'test']
        return self.tools['exec_cmd'].run(cmd, cwd=repo_path, timeout=1200)

    def _is_no_safe_patch(self, patch_text):
        return isinstance(patch_text, str) and 'NO_SAFE_PATCH' in patch_text

