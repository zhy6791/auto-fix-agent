"""AutoFixAgent Phase 3 implementation.

This module provides a single-agent pipeline for:
log reading -> stack trace parsing -> source locating -> LLM patch generation
-> patch validation -> optional local branch creation and patch application.
"""

import json
import difflib
import logging
import os
import re
from datetime import datetime

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows platforms
    winreg = None

from tools import file_io, exec_cmd, git_manager
from integrations.llm_client import LLMClient
from integrations.gitee_client import GiteeClient


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

        api_key = self._resolve_env_var(api_key_ref)
        if not api_key:
            raise ValueError('Missing LLM API key in environment variable: %s' % api_key_ref)

        return LLMClient(api_key=api_key, model=model, temperature=temperature, base_url=base_url, timeout=timeout)

    def _resolve_env_var(self, name):
        if not name:
            return ''

        value = os.environ.get(name)
        if value:
            return value

        if os.name != 'nt' or winreg is None:
            return ''

        registry_roots = [
            (winreg.HKEY_CURRENT_USER, r'Environment'),
            (winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'),
        ]

        for root, subkey in registry_roots:
            try:
                with winreg.OpenKey(root, subkey) as key:
                    value, _ = winreg.QueryValueEx(key, name)
                    if value:
                        return value
            except FileNotFoundError:
                continue
            except OSError:
                continue

        return ''

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
            'ci_result': None,
            'pr_result': None,
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
            if best_frame:
                # Traditional path: locate from stack trace
                source_info = self.find_source_location(repo_path, best_frame)
                detection_method = 'stack_trace'
            else:
                # New path: infer from exception message when no app frame found
                source_info = self._infer_from_exception_message(repo_path, raw_stack, parsed_stack)
                detection_method = 'exception_inference'
                if not source_info:
                    raise ValueError('Could not locate source from stack trace or exception message')
            
            report['located_files'] = [source_info]
            report['detection_method'] = detection_method

            prompt = self.build_prompt(raw_stack, source_info)
            report['prompt'] = prompt

            max_tokens = int(self.config.get('max_tokens', 8192))
            patch_text = self.llm_client.generate_patch(prompt, max_tokens=max_tokens)
            report['patch_text'] = patch_text

            if self._is_no_safe_patch(patch_text):
                raise ValueError(patch_text)

            validation = self.validate_patch(repo_path, patch_text, source_info=source_info)
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

                if apply_result.get('applied') and apply_result.get('files'):
                    # Commit changes on the fix branch so they stay isolated
                    commit_msg = 'fix: auto-fix %s in %s.%s' % (
                        parsed_stack[0].get('exception_type', 'Exception'),
                        source_info.get('class_name', ''),
                        source_info.get('method', ''),
                    )
                    committed = self.tools['git_manager'].commit_changes(
                        repo_path, commit_msg, files=apply_result['files']
                    )
                    report['apply_result']['committed'] = committed
                    logger.info("Committed on branch %s: %s", branch_name, committed)

                # Generate and apply test patch if enabled
                if apply_result.get('applied'):
                    test_gen_cfg = self.config.get('test_generation', {})
                    if test_gen_cfg.get('enabled', False) and test_gen_cfg.get('framework') == 'junit5':
                        try:
                            test_patch = self.generate_test_patch(source_info, report['patch_text'])
                            if not self._is_no_safe_patch(test_patch):
                                test_validation = self.validate_patch(repo_path, test_patch, source_info=source_info)
                                if test_validation['valid']:
                                    test_apply = self.tools['git_manager'].apply_patch(repo_path, test_patch)
                                    if test_apply.get('applied'):
                                        test_committed = self.tools['git_manager'].commit_changes(
                                            repo_path, 'test: auto-generate unit tests for %s' % source_info.get('method', 'test'),
                                            files=test_apply.get('files', [])
                                        )
                                        report['test_patch_applied'] = True
                                        report['test_generation_result'] = {'generated': True, 'files': test_apply.get('files', [])}
                                        logger.info("Generated and applied test patch: %s", test_apply.get('files', []))
                                    else:
                                        report['test_generation_result'] = {'generated': False, 'error': 'Failed to apply test patch'}
                                else:
                                    report['test_generation_result'] = {'generated': False, 'error': '; '.join(test_validation['errors'])}
                            else:
                                report['test_generation_result'] = {'generated': False, 'error': 'LLM cannot generate safe tests'}
                        except Exception as e:
                            logger.warning('Test generation failed: %s', str(e))
                            report['test_generation_result'] = {'generated': False, 'error': str(e)}

                if apply_result.get('applied'):
                    ci_result = self._run_ci_pipeline(
                        repo_path, source_info, raw_stack, parsed_stack,
                        report['patch_text'], report['prompt'],
                        initial_files=apply_result.get('files', [])
                    )
                    report['ci_result'] = ci_result

                    if self.config.get('gitee', {}).get('enabled', False) and \
                            'compile' in ci_result.get('stages_passed', []):
                        report['pr_result'] = self._push_and_create_pr(
                            repo_path, branch_name, parsed_stack, source_info, ci_result
                        )
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
            # Don't include line numbers in snippet - LLM may copy them as code!
            snippet_lines.append(source_lines[idx - 1])

        return {
            'source_path': source_path,
            'repo_relative_path': os.path.relpath(str(source_path), str(repo_root)),
            'class_name': class_name,
            'method': frame.get('method'),
            'line_no': line_no,
            'context_snippet': '\n'.join(snippet_lines),
            'full_source': source_text,  # Include complete file for LLM to understand context
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
        prompt.append('- 只修改上面给出的目标文件，且尽量只改问题行附近的局部代码')
        prompt.append('- 不要修改 package 声明、import 语句、类名、方法签名或文件路径，除非修复绝对依赖它们')
        prompt.append('- 不要猜测不存在的包名、类名或导入；如果不确定，返回 NO_SAFE_PATCH')
        prompt.append('- 优先使用 null check、边界检查等防御性编程')
        prompt.append('- 【重要】返回的 patched_content 必须是 COMPLETE 修复后的完整源文件，从 package 到最后一行')
        prompt.append('- 【重要】patched_content 必须可直接编译，不能包含任何代码片段、省略号(...)、行号前缀、或占位符')
        prompt.append('- 【重要】返回时保持原文件的 package、imports、类声明、所有方法完整')
        prompt.append('')
        
        # If this is an inferred source (not from stack trace), add extra context
        if source_info.get('inferred'):
            prompt.append('## 重要提示')
            prompt.append('本次异常是基于异常消息推断定位的源码位置，而不是从堆栈直接追踪。')
            prompt.append('推断理由：%s' % source_info.get('reasoning', 'N/A'))
            prompt.append('请特别注意：生成的补丁应该符合推断的问题描述，确保修复与异常消息一致。')
            prompt.append('')
        prompt.append('')
        prompt.append('## 异常信息')
        prompt.append('```')
        prompt.append(raw_stack[:500])  # Limit stack size
        prompt.append('```')
        prompt.append('')
        prompt.append('## 源码位置与问题上下文')
        prompt.append('文件: %s (repo 相对路径)' % source_info.get('repo_relative_path'))
        prompt.append('问题行: %s' % source_info.get('line_no'))
        prompt.append('方法: %s' % source_info.get('method'))
        prompt.append('')
        prompt.append('### 问题行附近代码（7-8 行窗口）:')
        prompt.append('```java')
        prompt.append(source_info.get('context_snippet', ''))
        prompt.append('```')
        prompt.append('')
        
        # Add complete file content
        full_source = source_info.get('full_source', '')
        if full_source:
            prompt.append('### 原始完整文件内容（作为修改基础）:')
            prompt.append('```java')
            # Limit full source to avoid token explosion, but keep package+imports+class+methods
            lines = full_source.splitlines()
            if len(lines) > 150:
                # Show first 100 lines and last 50 lines
                prompt.append('\n'.join(lines[:100]))
                prompt.append('... [中间部分省略] ...')
                prompt.append('\n'.join(lines[-50:]))
            else:
                prompt.append(full_source)
            prompt.append('```')
            prompt.append('')
        
        prompt.append('## 输出格式说明')
        prompt.append('你的任务是修改上面的完整文件内容，只改动需要修复的地方，然后返回修改后的完整文件。')
        prompt.append('')
        prompt.append('返回以下格式之一:')
        prompt.append('')
        prompt.append('### 选项 1: JSON 格式（推荐）')
        prompt.append('```json')
        prompt.append('{')
        prompt.append('  "files": [')
        prompt.append('    {')
        prompt.append('      "path": "src/main/java/com/fixflow/mall/service/OrderService.java",')
        prompt.append('      "patched_content": "package com.fixflow.mall.service;\\n\\nimport ...;\\n\\npublic class OrderService {\\n  ...完整修改后的所有方法...\\n}"')
        prompt.append('    }')
        prompt.append('  ]')
        prompt.append('}')
        prompt.append('```')
        prompt.append('')
        prompt.append('### 选项 2: 无法安全修复')
        prompt.append('如果无法生成安全补丁，返回: NO_SAFE_PATCH: 原因说明')
        return '\n'.join(prompt)

    def build_test_prompt(self, source_info, fix_patch_text):
        """Build LLM prompt for generating JUnit5 unit tests (Strategy B: target + boundaries + regression).
        
        Returns a prompt asking LLM to generate a test class with:
        - Exception reproduction test
        - Boundary value tests  
        - Regression tests for the method
        """
        lines = []
        lines.append('## 任务')
        lines.append('基于以下修复内容，生成一个 JUnit5 单元测试类来验证修复的正确性。')
        lines.append('')
        lines.append('## 要求')
        lines.append('- 测试框架：JUnit5（org.junit.jupiter.api）')
        lines.append('- 测试类名：以 "Test" 结尾（如 HelloControllerTest）')
        lines.append('- 生成 3-5 个测试用例，包括：')
        lines.append('  1. 异常复现：触发原异常的输入场景')
        lines.append('  2. 修复验证：验证修复后该场景不再报错')
        lines.append('  3. 边界值：null、空字符串、边界数值等')
        lines.append('  4. 回归测试：正常路径的功能验证')
        lines.append('- 使用 @Test、@DisplayName、@ParameterizedTest 等 JUnit5 注解')
        lines.append('- 必须使用 assertEquals、assertTrue、assertThrows 等断言')
        lines.append('- 完整的 package、imports、类声明、所有方法')
        lines.append('- 返回时保持原 package 和所有 imports 完整')
        lines.append('')
        lines.append('## 被修复的方法')
        lines.append('类名：%s' % source_info.get('class_name', 'Unknown'))
        lines.append('方法：%s' % source_info.get('method', 'unknown'))
        lines.append('文件：%s' % source_info.get('repo_relative_path', 'unknown'))
        lines.append('')
        lines.append('## 修复补丁内容（参考）')
        lines.append('```')
        lines.append(fix_patch_text[:1000])
        if len(fix_patch_text) > 1000:
            lines.append('... [补丁内容较长，摘要] ...')
        lines.append('```')
        lines.append('')
        lines.append('## 输出格式')
        lines.append('返回 JSON 格式：')
        lines.append('```json')
        lines.append('{')
        lines.append('  "files": [')
        lines.append('    {')
        lines.append('      "path": "src/test/java/com/example/demo/controller/HelloControllerTest.java",')
        lines.append('      "patched_content": "package com.example.demo.controller;\\n\\nimport org.junit.jupiter.api.*;\\n\\npublic class HelloControllerTest {\\n  @Test\\n  void testSayHelloWithNull() {\\n    ...\\n  }\\n}"')
        lines.append('    }')
        lines.append('  ]')
        lines.append('}')
        lines.append('```')
        lines.append('')
        lines.append('若无法生成安全测试，返回: NO_SAFE_PATCH: 原因说明')
        
        return '\n'.join(lines)

    def generate_test_patch(self, source_info, fix_patch_text):
        """Generate JUnit5 test patch via LLM."""
        test_prompt = self.build_test_prompt(source_info, fix_patch_text)
        max_tokens = int(self.config.get('max_tokens', 8192))
        test_patch_text = self.llm_client.generate_patch(test_prompt, max_tokens=max_tokens)
        return test_patch_text

    def validate_patch(self, repo_path, patch_text, source_info=None):
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
                            struct_errors = self._validate_java_structure(old_text, patched_content, int(line_no) if line_no else None)
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

    def _changes_are_localized(self, old_text, new_text, line_no, window=8):
        """Return True when line changes stay close to the analyzed line.
        
        Checks:
        1. package/import statements are not modified (critical Java structure)
        2. All code changes fall within [line_no - window, line_no + window]
        """
        if not line_no:
            return True

        old_lines = old_text.splitlines()
        new_lines = new_text.splitlines()

        # 1) Reject any modification to package or import statements
        def _top_section(lines):
            pkg = None
            imports = []
            for ln in lines:
                s = ln.strip()
                if not s:
                    continue
                if s.startswith('package '):
                    pkg = s
                elif s.startswith('import '):
                    imports.append(s)
                else:
                    break
            return pkg, imports

        old_pkg, old_imports = _top_section(old_lines)
        new_pkg, new_imports = _top_section(new_lines)
        if old_pkg != new_pkg or old_imports != new_imports:
            return False

        # 2) Ensure all non-header changes fall within window
        matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines)
        lower = max(1, int(line_no) - int(window))
        upper = int(line_no) + int(window)

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == 'equal':
                continue
            
            # Skip opcode if it only touches the import/package section (lines 1-20)
            # since we already validated those above
            if i2 <= 20 and j2 <= 20:
                continue
            
            # Check that changes in body fall within window
            for line_index in range(i1 + 1, i2 + 1):
                if line_index > 20 and (line_index < lower or line_index > upper):
                    return False
            for line_index in range(j1 + 1, j2 + 1):
                if line_index > 20 and (line_index < lower or line_index > upper):
                    return False

        return True

    def _make_branch_name(self):
        prefix = self.config.get('branch_prefix', 'fix/')
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        return '%sauto-%s' % (prefix, timestamp)

    @staticmethod
    def _resolve_build_cmd(repo_path, build_tool, *args):
        """Return [executable, ...args] preferring project wrapper scripts.

        Checks for mvnw/mvnw.cmd (Maven) or gradlew/gradlew.bat (Gradle) in
        repo_path first, then tries the system command with .cmd/.bat
        extensions on Windows before falling back to the bare name.
        """
        import shutil

        if build_tool == 'gradle':
            wrapper_candidates = ['gradlew.bat', 'gradlew']
            system_candidates = ['gradle.bat', 'gradle.cmd', 'gradle']
        else:
            wrapper_candidates = ['mvnw.cmd', 'mvnw']
            system_candidates = ['mvn.cmd', 'mvn.bat', 'mvn']

        for name in wrapper_candidates:
            p = os.path.join(repo_path, name)
            if os.path.isfile(p):
                return [p] + list(args)

        for name in system_candidates:
            if shutil.which(name):
                return [name] + list(args)

        return [system_candidates[-1]] + list(args)

    def _run_compile(self, repo_path):
        """Run compile-only check (no tests)."""
        build_tool = self.config.get('java_build', 'maven')
        if build_tool == 'gradle':
            cmd = self._resolve_build_cmd(repo_path, 'gradle', 'compileJava')
        else:
            cmd = self._resolve_build_cmd(repo_path, 'maven', 'compile', '-q')
        return self.tools['exec_cmd'].run(cmd, cwd=repo_path, timeout=600)

    def _run_tests(self, repo_path):
        """Run unit tests."""
        build_tool = self.config.get('java_build', 'maven')
        if build_tool == 'gradle':
            cmd = self._resolve_build_cmd(repo_path, 'gradle', 'test')
        else:
            cmd = self._resolve_build_cmd(repo_path, 'maven', 'test', '-q')
        return self.tools['exec_cmd'].run(cmd, cwd=repo_path, timeout=1200)

    @staticmethod
    def _is_tool_missing(build_result):
        """Return True if the build failed because the tool itself is missing."""
        stderr = (build_result.get('stderr', '') or '').lower()
        return 'command not found' in stderr or 'filenotfounderror' in stderr.replace(' ', '')

    def _run_ci_pipeline(self, repo_path, source_info, raw_stack, parsed_stack,
                         original_patch_text, original_prompt, initial_files=None):
        """Run compile then tests, with LLM retry on failure.

        Returns dict: {compile_result, test_result, retries_used, patch_history,
                       stages_passed, stages_failed}
        """
        max_retries = int(self.config.get('max_retries', 3))
        run_compile = self.config.get('run_compile_on_apply', False)
        run_tests = self.config.get('run_tests_on_apply', False)

        ci_result = {
            'compile_result': None,
            'test_result': None,
            'retries_used': 0,
            'patch_history': [],
            'stages_passed': [],
            'stages_failed': [],
        }

        if not run_compile and not run_tests:
            return ci_result

        current_patch = original_patch_text
        current_prompt = original_prompt
        retry_files = []
        first_retry = True

        # Stage 1: Compile with retry
        if run_compile:
            for attempt in range(max_retries + 1):
                compile_result = self._run_compile(repo_path)
                if compile_result['code'] == 0:
                    ci_result['compile_result'] = compile_result
                    ci_result['stages_passed'].append('compile')
                    break

                # System-level failures cannot be fixed by LLM
                if self._is_tool_missing(compile_result):
                    ci_result['compile_result'] = compile_result
                    ci_result['stages_failed'].append('compile')
                    ci_result['patch_history'].append({
                        'stage': 'compile',
                        'attempt': attempt + 1,
                        'error': 'Build tool not installed: %s'
                                 % (compile_result.get('stderr', '') or '').strip(),
                    })
                    break

                ci_result['patch_history'].append({
                    'stage': 'compile',
                    'attempt': attempt + 1,
                    'error': (compile_result.get('stderr', '') or '')[:2000],
                })
                if attempt >= max_retries:
                    ci_result['compile_result'] = compile_result
                    ci_result['stages_failed'].append('compile')
                    break

                # Only count actual LLM retries (not the first failed attempt)
                ci_result['retries_used'] += 1
                new_patch = self._retry_with_feedback(
                    current_prompt, source_info, 'compile',
                    compile_result, repo_path
                )
                if new_patch is None:
                    ci_result['stages_failed'].append('compile')
                    break
                # Revert previously patched files before applying retry
                revert_files = initial_files if first_retry else retry_files
                if revert_files:
                    self._revert_files(repo_path, revert_files, ref='HEAD~1')
                first_retry = False
                apply_result = self.tools['git_manager'].apply_patch(repo_path, new_patch)
                retry_files = apply_result.get('files', [])
                current_patch = new_patch

        # Stage 2: Tests (only if compile passed)
        if run_tests and 'compile' not in ci_result['stages_failed']:
            for attempt in range(max_retries + 1):
                test_result = self._run_tests(repo_path)
                if test_result['code'] == 0:
                    ci_result['test_result'] = test_result
                    ci_result['stages_passed'].append('tests')
                    break

                if self._is_tool_missing(test_result):
                    ci_result['test_result'] = test_result
                    ci_result['stages_failed'].append('tests')
                    ci_result['patch_history'].append({
                        'stage': 'tests',
                        'attempt': attempt + 1,
                        'error': 'Test tool not installed: %s'
                                 % (test_result.get('stderr', '') or '').strip(),
                    })
                    break

                ci_result['patch_history'].append({
                    'stage': 'tests',
                    'attempt': attempt + 1,
                    'error': (test_result.get('stderr', '') or '')[:2000],
                })
                if attempt >= max_retries:
                    ci_result['test_result'] = test_result
                    ci_result['stages_failed'].append('tests')
                    break

                ci_result['retries_used'] += 1
                new_patch = self._retry_with_feedback(
                    current_prompt, source_info, 'tests',
                    test_result, repo_path
                )
                if new_patch is None:
                    ci_result['stages_failed'].append('tests')
                    break
                # Revert previously patched files before applying retry
                if retry_files:
                    self._revert_files(repo_path, retry_files, ref='HEAD~1')
                apply_result = self.tools['git_manager'].apply_patch(repo_path, new_patch)
                retry_files = apply_result.get('files', [])
                current_patch = new_patch

        if ci_result['retries_used'] > 0 and current_patch != original_patch_text and retry_files:
            self.tools['git_manager'].commit_changes(
                repo_path,
                'fix: retry patch (%d LLM retries)' % ci_result['retries_used'],
                files=list(set(retry_files))
            )

        return ci_result

    def _revert_files(self, repo_path, files, ref='HEAD'):
        """Revert specified files to a given git ref via git checkout."""
        if not files:
            return
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
            logger.info("Reverted %d files to %s", len(rel_files), ref)
        except subprocess.CalledProcessError as e:
            logger.warning("Failed to revert files to %s: %s", ref, e)

    def _retry_with_feedback(self, original_prompt, source_info, failed_stage,
                             build_result, repo_path):
        """Feed build/test failure back to LLM for a corrected patch.

        Returns new patch text, or None if LLM cannot fix.
        """
        error_output = (build_result.get('stderr', '') or '')[:3000]
        if not error_output:
            error_output = (build_result.get('stdout', '') or '')[:3000]

        retry_prompt = self._build_retry_prompt(
            original_prompt, source_info, failed_stage, error_output
        )

        max_tokens = int(self.config.get('max_tokens', 8192))
        new_patch_text = self.llm_client.generate_patch(retry_prompt, max_tokens=max_tokens)

        if self._is_no_safe_patch(new_patch_text):
            return None

        validation = self.validate_patch(repo_path, new_patch_text, source_info=source_info)
        if not validation['valid']:
            logger.warning('Retry patch failed validation: %s', '; '.join(validation['errors']))
            return None

        return new_patch_text

    def _build_retry_prompt(self, original_prompt, source_info, failed_stage, error_output):
        """Build a prompt asking the LLM to fix compile/test failures."""
        stage_cn = '编译' if failed_stage == 'compile' else '单元测试'

        lines = []
        lines.append('## 修复任务：你的上一个补丁导致了%s失败' % stage_cn)
        lines.append('')
        lines.append('请分析下面的错误输出，并生成一个新的修复补丁。')
        lines.append('')
        lines.append('## 原始修复任务（上下文）')
        lines.append(original_prompt)
        lines.append('')
        lines.append('## %s 错误输出' % stage_cn)
        lines.append('```')
        lines.append(error_output)
        lines.append('```')
        lines.append('')
        lines.append('## 要求')
        lines.append('- 修复导致 %s 失败的问题' % stage_cn)
        lines.append('- 保持原来针对异常的正确修复不丢失')
        lines.append('- 返回格式与之前相同：JSON 格式的补丁')
        lines.append('- 如果无法同时满足编译和修复异常，优先保证编译通过')

        return '\n'.join(lines)

    def _push_and_create_pr(self, repo_path, branch_name, parsed_stack, source_info,
                             ci_result):
        """Push branch to remote and create a Gitee PR.

        Returns dict: {pr_created: bool, pr_url: str, pr_number: int, error: str}
        """
        result = {'pr_created': False, 'pr_url': '', 'pr_number': None, 'error': None}

        gitee_cfg = self.config.get('gitee', {}) or {}
        if not gitee_cfg.get('enabled', False):
            result['error'] = 'Gitee integration not enabled'
            return result

        # Check if tests are required and if they passed
        test_gen_cfg = self.config.get('test_generation', {})
        if test_gen_cfg.get('enabled', False) and gitee_cfg.get('require_tests_to_pass_for_pr', True):
            if 'tests' not in ci_result.get('stages_passed', []):
                result['error'] = 'Tests must pass before PR can be created (tests failed or skipped)'
                return result

        owner = gitee_cfg.get('owner', '')

        repo = gitee_cfg.get('repo', '')
        if not owner or not repo:
            remote_url = self.tools['git_manager'].get_remote_url(repo_path)
            parsed_owner, parsed_repo = self.tools['git_manager'].parse_gitee_owner_repo(remote_url)
            owner = owner or parsed_owner
            repo = repo or parsed_repo

        if not owner or not repo:
            result['error'] = 'Could not determine Gitee owner/repo. Configure gitee.owner and gitee.repo in config.yml'
            return result

        pushed = self.tools['git_manager'].push_branch(repo_path, branch_name)
        if not pushed:
            result['error'] = 'Failed to push branch to remote'
            return result

        token_env = gitee_cfg.get('access_token_env', 'GITEE_TOKEN')
        access_token = self._resolve_env_var(token_env)
        if not access_token:
            result['error'] = 'Gitee access token not configured (set access_token_env)'
            return result

        gitee_client = GiteeClient(
            access_token=access_token,
            base_url=gitee_cfg.get('api_base_url', 'https://gitee.com/api/v5')
        )

        exception_type = parsed_stack[0].get('exception_type', 'Exception') if parsed_stack else 'Exception'
        class_name = source_info.get('class_name', 'unknown') if source_info else 'unknown'
        title_template = gitee_cfg.get('pr_title_template', 'fix: auto-fix {exception_type} in {class_name}')
        title = title_template.format(exception_type=exception_type, class_name=class_name)

        body_lines = [
            '## Auto-Fix PR',
            '',
            'This PR was automatically generated by auto-fix-agent.',
            '',
            '### Exception',
            '```',
            parsed_stack[0].get('exception_type', 'Unknown') if parsed_stack else 'Unknown',
            '```',
            '',
            '### CI Pipeline',
        ]
        for stage in ci_result.get('stages_passed', []):
            body_lines.append('- [OK] %s' % stage)
        for stage in ci_result.get('stages_failed', []):
            body_lines.append('- [FAIL] %s' % stage)
        body = '\n'.join(body_lines)

        target_branch = gitee_cfg.get('target_branch', 'main')

        pr_result = gitee_client.create_pull_request(
            owner=owner, repo=repo, title=title,
            head=branch_name, base=target_branch, body=body
        )

        result['pr_created'] = pr_result['success']
        result['pr_url'] = pr_result.get('url', '')
        result['pr_number'] = pr_result.get('number')
        result['error'] = pr_result.get('error')

        return result

    def _is_no_safe_patch(self, patch_text):
        return isinstance(patch_text, str) and 'NO_SAFE_PATCH' in patch_text

    def _validate_java_structure(self, old_text, new_text, line_no, window=8):
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
            
            for idx, ln in enumerate(lines):
                s = ln.strip()
                if not s or s.startswith('//'):
                    continue
                if s.startswith('package '):
                    pkg = s
                    pkg_line = idx
                elif s.startswith('import '):
                    imports.append(s)
                    import_end = idx
                elif s.startswith('import ') or (pkg is not None and not s.startswith('import')):
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
        method_sig_re = re.compile(r'^\s*(public|protected|private)\s+[\w<>,\s\[\]]+\s+([\w$]+)\s*\(')
        
        def _extract_methods(lines):
            methods = {}
            for idx, ln in enumerate(lines):
                m = method_sig_re.match(ln)
                if m:
                    methods[m.group(2)] = idx + 1  # 1-based line number
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
        # Check for pattern like "123: code" or "59: //..." which indicates LLM copied snippet format
        line_no_pattern = re.compile(r'^\s*\d+:\s+')
        code_lines_with_prefix = 0
        for ln in new_lines:
            stripped = ln.strip()
            if stripped and not stripped.startswith('//') and not stripped.startswith('*') and line_no_pattern.match(stripped):
                code_lines_with_prefix += 1
        
        if code_lines_with_prefix > 0:
            errors.append('CRITICAL: Patched code contains line-number prefixes (LLM copied snippet format). %d lines with prefix detected. This is NOT valid Java code.' % code_lines_with_prefix)
        
        return errors

    def _infer_from_exception_message(self, repo_path, raw_stack, parsed_stack):
        """When stack has no app frames, let LLM infer the source location.
        
        This handles framework-level exceptions (MissingPathVariableException,
        BindingException, ValidationException, etc.) where the real bug is in
        application code but the stack only shows framework code.
        
        Strategy:
        1. Send raw exception to LLM with special "inference" prompt
        2. LLM analyzes exception message and returns suspected source location
        3. Verify the returned location exists before proceeding
        
        Returns source_info dict or None if inference fails.
        """
        try:
            logger.info('Attempting to infer source location from exception message')
            
            # Build inference prompt
            inference_prompt = self._build_inference_prompt(repo_path, raw_stack)
            
            max_tokens = int(self.config.get('max_tokens', 8192))
            llm_response = self.llm_client.generate_patch(inference_prompt, max_tokens=max_tokens)
            
            logger.debug('LLM inference response: %s', llm_response[:500])
            
            # Parse LLM response for suspected file path and method
            source_info = self._parse_inference_response(repo_path, llm_response)
            
            if source_info:
                logger.info('Successfully inferred source location: %s', source_info.get('repo_relative_path'))
            else:
                logger.warning('Failed to parse or verify inferred source location')
            
            return source_info
        except Exception as e:
            logger.error('Exception during source inference: %s', e)
            return None

    def _build_inference_prompt(self, repo_path, raw_stack):
        """Build a special prompt for LLM to infer source location from exception."""
        
        # Scan repo structure to help LLM understand codebase
        app_packages = self._scan_app_packages(repo_path)
        
        prompt = []
        prompt.append('## 任务：从异常消息反向定位源码位置')
        prompt.append('')
        prompt.append('你收到了一个 Java 异常，其堆栈主要是框架代码（Spring/Tomcat/Jakarta），')
        prompt.append('但真正的 BUG 在应用代码中。请根据异常消息分析并推断：')
        prompt.append('1. 最可能的源文件（完整路径或类名）')
        prompt.append('2. 最可能的方法名')
        prompt.append('3. 问题的简要说明')
        prompt.append('')
        prompt.append('## 项目结构')
        if app_packages:
            prompt.append('应用包名: %s' % ', '.join(app_packages[:5]))
        else:
            prompt.append('应用包名: (无法扫描，请自动推断)')
        prompt.append('')
        prompt.append('## 异常信息')
        prompt.append('```')
        prompt.append(raw_stack[:1200])
        prompt.append('```')
        prompt.append('')
        prompt.append('## 输出格式')
        prompt.append('返回 JSON:')
        prompt.append('```json')
        prompt.append('{')
        prompt.append('  "suspected_file": "src/main/java/com/fixflow/mall/api/MallController.java",')
        prompt.append('  "suspected_class": "com.fixflow.mall.api.MallController",')
        prompt.append('  "suspected_method": "getOrder",')
        prompt.append('  "reasoning": "MissingPathVariableException 说缺少 id 参数，likely @PathVariable 绑定错误"')
        prompt.append('}')
        prompt.append('```')
        prompt.append('')
        prompt.append('只返回 JSON，不要返回其他内容。')
        
        return '\n'.join(prompt)

    def _scan_app_packages(self, repo_path):
        """Scan repo to identify main application packages."""
        packages = set()
        repo_root = os.path.abspath(repo_path)
        src_paths = [
            os.path.join(repo_root, 'src', 'main', 'java'),
            os.path.join(repo_root, 'src', 'main', 'kotlin'),
        ]
        
        for src_path in src_paths:
            if os.path.isdir(src_path):
                for root, dirs, files in os.walk(src_path):
                    # Get package path
                    rel = os.path.relpath(root, src_path)
                    if rel != '.' and not rel.startswith('.'):
                        pkg = rel.replace(os.sep, '.').strip('.')
                        if pkg:
                            packages.add(pkg)
        
        return sorted(list(packages))[:10]

    def _parse_inference_response(self, repo_path, llm_response):
        """Parse LLM's inference response and verify source location."""
        try:
            # Extract JSON from response
            if '```json' in llm_response:
                start = llm_response.find('{')
                end = llm_response.rfind('}') + 1
                if start >= 0 and end > start:
                    json_str = llm_response[start:end]
                else:
                    json_str = llm_response
            else:
                json_str = llm_response
            
            inferred = json.loads(json_str)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning('Could not parse LLM inference response: %s', llm_response[:200])
            return None
        
        suspected_file = inferred.get('suspected_file', '')
        suspected_class = inferred.get('suspected_class', '')
        suspected_method = inferred.get('suspected_method', '')
        reasoning = inferred.get('reasoning', '')
        
        # Try to locate the file
        source_path = self._locate_file_by_class_or_path(repo_path, suspected_class, suspected_file)
        
        if not source_path or not os.path.exists(source_path):
            logger.warning('Could not verify inferred file: %s / %s', suspected_file, suspected_class)
            return None
        
        # Read file and extract context
        try:
            content = self.tools['file_io'].read_file(str(source_path))
            lines = content.splitlines()
            
            # Find method if possible
            method_line = self._find_method_line(lines, suspected_method) if suspected_method else None
            if method_line is None:
                method_line = max(1, len(lines) // 2)  # Fallback to middle
            
            # Extract context around suspected line
            start = max(0, method_line - 4)
            end = min(len(lines), method_line + 10)
            snippet = '\n'.join(lines[start:end])
            
            repo_root = os.path.abspath(repo_path)
            rel_path = os.path.relpath(str(source_path), repo_root)
            
            return {
                'source_path': source_path,
                'repo_relative_path': rel_path,
                'class_name': suspected_class.split('.')[-1],
                'method': suspected_method,
                'line_no': method_line,
                'context_snippet': snippet,
                'full_source': content,
                'inferred': True,  # Mark as inferred, not from stack
                'reasoning': reasoning,
            }
        except Exception as e:
            logger.error('Error reading inferred file: %s', e)
            return None

    def _locate_file_by_class_or_path(self, repo_path, class_name, file_path):
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

    def _find_method_line(self, lines, method_name):
        """Find the line number of a method definition."""
        if not method_name:
            return None
        
        # Match: public/private/protected ... methodName(
        pattern = re.compile(r'(?:public|private|protected)?\s+\w+\s+' + re.escape(method_name) + r'\s*\(')
        for idx, line in enumerate(lines):
            if pattern.search(line):
                return idx + 1  # 1-based line number
        
        return None

