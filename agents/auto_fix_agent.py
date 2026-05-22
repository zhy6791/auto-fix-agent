"""AutoFixAgent — pipeline orchestrator.

Delegates to specialized modules for stack parsing, source location,
prompt building, patch validation, CI pipeline, exception inference,
and PR management.
"""

import logging
import os
from datetime import datetime

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows platforms
    winreg = None

from tools import file_io, exec_cmd, git_manager
from integrations.llm_client import LLMClient
from agents import (
    stacktrace_parser,
    source_locator,
    prompt_builder,
    patch_validator,
    ci_pipeline,
    exception_inference,
    pr_manager,
    tool_registry,
    react_agent,
)

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
        self.tool_registry = tool_registry.ToolRegistry(
            self.config, self.tools, self.llm_client
        )

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
        # 高级流水线步骤（内联注释说明）：
        # 1) 准备报告骨架
        # 2) 从日志读取并解析最新异常块
        # 3) 定位源码：优先使用堆栈帧，无法定位时退回到 LLM 推断
        # 4) 构建 LLM 提示并请求补丁
        # 5) 使用多层校验验证补丁
        # 6) 如果非 dry-run：创建分支、应用补丁、提交、生成测试、运行 CI、创建 PR
        # 7) 返回最终报告（或错误信息）
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

            # ── Stage 1: 异常提取 ──
            logger.info('[1/6] 提取异常堆栈...')
            raw_stack = self._read_latest_stack(logs_path)
            report['raw_stack'] = raw_stack

            parsed_stack = self.parse_stacktrace(raw_stack)
            report['parsed_stack'] = parsed_stack

            if not parsed_stack:
                raise ValueError('No stack trace entries parsed from logs')

            exc_type = parsed_stack[0].get('exception_type', 'Exception')
            top_frame = '%s.%s():%s' % (
                parsed_stack[0].get('class_name', '?'),
                parsed_stack[0].get('method', '?'),
                parsed_stack[0].get('line_no', '?'),
            )
            logger.info('  异常: %s  顶层帧: %s', exc_type, top_frame)

            # ── Stage 2: Agent 决策循环 ──
            logger.info('[2/6] Agent 决策循环...')
            max_iterations = int(self.config.get('max_agent_iterations', 10))
            agent = react_agent.ReActAgent(
                self.config, self.tool_registry, self.llm_client,
                max_iterations=max_iterations,
            )
            agent_result = agent.run({
                'raw_stack': raw_stack,
                'parsed_stack': parsed_stack,
                'repo_path': repo_path,
                'dry_run': dry_run,
            })

            report['agent_iterations'] = agent_result.get('iterations', 0)
            report['agent_thoughts'] = agent_result.get('thoughts', [])
            report['agent_tool_calls'] = agent_result.get('tool_calls', [])

            if agent_result.get('aborted'):
                raise ValueError('Agent aborted: %s' % agent_result.get('abort_reason', 'Unknown'))

            patch_text = agent_result.get('final_patch', '')
            source_info = agent_result.get('source_info', {})

            if not patch_text:
                raise ValueError('Agent did not produce a patch')

            report['located_files'] = [source_info] if source_info else []
            report['patch_text'] = patch_text
            report['detection_method'] = source_info.get('detection_method', 'agent')
            report['prompt'] = ''

            logger.info('  完成: %d 轮迭代, %d 次工具调用',
                        agent_result.get('iterations', 0),
                        len(agent_result.get('tool_calls', [])))

            # ── Stage 3: 补丁校验 ──
            logger.info('[3/6] 补丁校验...')
            validation = self.validate_patch(repo_path, patch_text, source_info=source_info)
            if not validation['valid']:
                raise ValueError('Patch validation failed: %s' % '; '.join(validation['errors']))
            logger.info('  校验通过')

            branch_name = self._make_branch_name()
            report['branch_name'] = branch_name

            if not dry_run:
                # ── Stage 4: 应用补丁 ──
                logger.info('[4/6] 应用补丁...')
                branch_ok = self.tools['git_manager'].create_branch(repo_path, branch_name)
                if not branch_ok:
                    raise RuntimeError('Failed to create branch: %s' % branch_name)
                logger.info('  分支: %s', branch_name)

                apply_result = self.tools['git_manager'].apply_patch(repo_path, patch_text)
                apply_result['dry_run'] = False
                report['apply_result'] = apply_result

                if apply_result.get('applied') and apply_result.get('files'):
                    commit_msg = 'fix: auto-fix %s in %s.%s' % (
                        parsed_stack[0].get('exception_type', 'Exception'),
                        source_info.get('class_name', ''),
                        source_info.get('method', ''),
                    )
                    committed = self.tools['git_manager'].commit_changes(
                        repo_path, commit_msg, files=apply_result['files']
                    )
                    report['apply_result']['committed'] = committed
                    logger.info('  已提交: %s (%d 文件)',
                                commit_msg, len(apply_result.get('files', [])))

                # ── Stage 5: 测试生成（可选）──
                if apply_result.get('applied'):
                    test_gen_cfg = self.config.get('test_generation', {})
                    if test_gen_cfg.get('enabled', False) and test_gen_cfg.get('framework') == 'junit5':
                        logger.info('[5/6] 生成 JUnit5 测试...')
                        try:
                            test_patch = self.generate_test_patch(source_info, report['patch_text'])
                            if not self._is_no_safe_patch(test_patch):
                                test_validation = self.validate_patch(repo_path, test_patch, source_info=source_info)
                                if test_validation['valid']:
                                    test_apply = self.tools['git_manager'].apply_patch(repo_path, test_patch)
                                    # If apply failed, delete existing test files and retry
                                    if not test_apply.get('applied') and test_apply.get('errors'):
                                        logger.info('  测试补丁首次应用失败，尝试清理已有测试文件后重试...')
                                        for f in test_validation.get('files', []):
                                            abs_f = os.path.join(repo_path, f)
                                            if os.path.exists(abs_f):
                                                os.remove(abs_f)
                                                logger.info('  已删除: %s', f)
                                        test_apply = self.tools['git_manager'].apply_patch(repo_path, test_patch)
                                    if test_apply.get('applied'):
                                        test_committed = self.tools['git_manager'].commit_changes(
                                            repo_path, 'test: auto-generate unit tests for %s' % source_info.get('method', 'test'),
                                            files=test_apply.get('files', [])
                                        )
                                        report['test_patch_applied'] = True
                                        report['test_generation_result'] = {'generated': True, 'files': test_apply.get('files', [])}
                                        logger.info('  测试已生成: %s', test_apply.get('files', []))
                                    else:
                                        errs = '; '.join(test_apply.get('errors', []))
                                        logger.warning('  测试补丁应用失败: %s', errs)
                                        report['test_generation_result'] = {'generated': False, 'error': 'Failed to apply: %s' % errs}
                                else:
                                    report['test_generation_result'] = {'generated': False, 'error': '; '.join(test_validation['errors'])}
                            else:
                                report['test_generation_result'] = {'generated': False, 'error': 'LLM cannot generate safe tests'}
                        except Exception as e:
                            logger.warning('  测试生成失败: %s', str(e))
                            report['test_generation_result'] = {'generated': False, 'error': str(e)}

                if apply_result.get('applied'):
                    # ── Stage 6: CI 管道 ──
                    logger.info('[6/6] CI 管道...')
                    ci_result = self._run_ci_pipeline(
                        repo_path, source_info, raw_stack, parsed_stack,
                        report['patch_text'], report['prompt'],
                        initial_files=apply_result.get('files', [])
                    )
                    report['ci_result'] = ci_result

                    for stage in ci_result.get('stages_passed', []):
                        logger.info('  ✔ %s', stage)
                    for stage in ci_result.get('stages_failed', []):
                        logger.info('  ✘ %s', stage)
                    if ci_result.get('retries_used', 0) > 0:
                        logger.info('  ↻ LLM 重试: %d 次', ci_result['retries_used'])

                    if self.config.get('gitee', {}).get('enabled', False) and \
                            'compile' in ci_result.get('stages_passed', []):
                        logger.info('  创建 Gitee PR...')
                        pr_result = self._push_and_create_pr(
                            repo_path, branch_name, parsed_stack, source_info, ci_result
                        )
                        report['pr_result'] = pr_result
                        if pr_result.get('pr_created'):
                            logger.info('  PR: %s', pr_result.get('pr_url', ''))
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

    def _make_branch_name(self):
        prefix = self.config.get('branch_prefix', 'fix/')
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        return '%sauto-%s' % (prefix, timestamp)

    # ── Delegation: stack trace parsing ──────────────────────────────

    def _read_latest_stack(self, logs_path):
        _, chunk = self.tools['file_io'].tail_file(logs_path)
        return self.extract_latest_exception_block(chunk)

    def extract_latest_exception_block(self, text):
        return stacktrace_parser.extract_latest_exception_block(text)

    def parse_stacktrace(self, text):
        return stacktrace_parser.parse_stacktrace(text)

    # ── Delegation: source location ──────────────────────────────────

    def _select_best_frame(self, repo_path, parsed_stack):
        return source_locator.select_best_frame(repo_path, parsed_stack, self.find_source_location)

    def find_source_location(self, repo_path, frame):
        return source_locator.find_source_location(repo_path, frame, self.tools['file_io'])

    def _locate_file_by_class_or_path(self, repo_path, class_name, file_path):
        return source_locator.locate_file_by_class_or_path(repo_path, class_name, file_path)

    def _find_method_line(self, lines, method_name):
        return source_locator.find_method_line(lines, method_name)

    # ── Delegation: prompt building ──────────────────────────────────

    def build_prompt(self, raw_stack, source_info):
        return prompt_builder.build_prompt(self.config, raw_stack, source_info)

    def build_test_prompt(self, source_info, fix_patch_text):
        return prompt_builder.build_test_prompt(source_info, fix_patch_text)

    def generate_test_patch(self, source_info, fix_patch_text):
        return prompt_builder.generate_test_patch(source_info, fix_patch_text, self.config, self.llm_client)

    def _build_retry_prompt(self, original_prompt, source_info, failed_stage, error_output):
        return prompt_builder.build_retry_prompt(original_prompt, source_info, failed_stage, error_output)

    # ── Delegation: patch validation ─────────────────────────────────

    def validate_patch(self, repo_path, patch_text, source_info=None):
        return patch_validator.validate_patch(self.config, repo_path, patch_text, source_info, self.tools['file_io'])

    @staticmethod
    def _is_no_safe_patch(patch_text):
        return patch_validator.is_no_safe_patch(patch_text)

    @staticmethod
    def _estimate_changed_lines(old_text, new_text):
        return patch_validator.estimate_changed_lines(old_text, new_text)

    @staticmethod
    def _validate_java_structure(old_text, new_text, line_no, window=8):
        return patch_validator.validate_java_structure(old_text, new_text, line_no, window)

    # ── Delegation: CI pipeline ──────────────────────────────────────

    @staticmethod
    def _resolve_build_cmd(repo_path, build_tool, *args):
        return ci_pipeline.resolve_build_cmd(repo_path, build_tool, *args)

    def _run_compile(self, repo_path):
        return ci_pipeline.run_compile(repo_path, self.config, self.tools['exec_cmd'])

    def _run_tests(self, repo_path):
        return ci_pipeline.run_tests(repo_path, self.config, self.tools['exec_cmd'])

    @staticmethod
    def _is_tool_missing(build_result):
        return ci_pipeline.is_tool_missing(build_result)

    def _run_ci_pipeline(self, repo_path, source_info, raw_stack, parsed_stack,
                         original_patch_text, original_prompt, initial_files=None):
        return ci_pipeline.run_ci_pipeline(
            repo_path, source_info, raw_stack, parsed_stack,
            original_patch_text, original_prompt,
            self.config, self.tools, self.llm_client, self.validate_patch,
            initial_files=initial_files,
            compile_fn=self._run_compile,
            test_fn=self._run_tests,
        )

    def _revert_files(self, repo_path, files, ref='HEAD'):
        return ci_pipeline.revert_files(repo_path, files, self.tools['git_manager'], ref)

    def _retry_with_feedback(self, original_prompt, source_info, failed_stage,
                             build_result, repo_path):
        return ci_pipeline.retry_with_feedback(
            original_prompt, source_info, failed_stage, build_result, repo_path,
            self.config, self.llm_client, self.validate_patch
        )

    # ── Delegation: exception inference ──────────────────────────────

    def _infer_from_exception_message(self, repo_path, raw_stack, parsed_stack):
        return exception_inference.infer_from_exception_message(
            repo_path, raw_stack, parsed_stack, self.config, self.llm_client, self.tools['file_io']
        )

    def _build_inference_prompt(self, repo_path, raw_stack):
        app_packages = exception_inference.scan_app_packages(repo_path)
        return exception_inference.build_inference_prompt(repo_path, raw_stack, app_packages)

    def _scan_app_packages(self, repo_path):
        return exception_inference.scan_app_packages(repo_path)

    def _parse_inference_response(self, repo_path, llm_response):
        return exception_inference.parse_inference_response(
            repo_path, llm_response, self.tools['file_io'], source_locator.locate_file_by_class_or_path
        )

    # ── Delegation: PR management ────────────────────────────────────

    def _push_and_create_pr(self, repo_path, branch_name, parsed_stack, source_info, ci_result):
        return pr_manager.push_and_create_pr(
            repo_path, branch_name, parsed_stack, source_info, ci_result,
            self.config, self.tools, self._resolve_env_var
        )
