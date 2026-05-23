"""AutoFixAgent — pipeline orchestrator.

Delegates to specialized modules for stack parsing, source location,
prompt building, patch validation, CI pipeline, exception inference,
and PR management.
"""

import logging
import os
import sys
from datetime import datetime

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows platforms
    winreg = None

from tools import file_io, exec_cmd, git_manager
from integrations.llm_client import LLMClient
from agents.log_extraction import stacktrace_parser, source_locator, exception_inference
from agents.agent_loop import react_agent, tool_registry, prompt_builder
from agents.post_processing import patch_validator, test_generator
from agents.ci import ci_pipeline, pr_manager
logger = logging.getLogger(__name__)


def _print_step_header(step_num, total, title):
    """Print a colored step header with timestamp."""
    timestamp = datetime.now().strftime('%H:%M:%S')
    # Use ANSI colors: cyan for header, gray for timestamp
    sys.stderr.write('\n')
    sys.stderr.write('\033[36m════════════════════════════════════════════════════════════\033[0m\n')
    sys.stderr.write('\033[1;36m  [%d/%d] %s\033[0m  \033[90m%s\033[0m\n' % (step_num, total, title, timestamp))
    sys.stderr.write('\033[36m════════════════════════════════════════════════════════════\033[0m\n')
    sys.stderr.flush()


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
            self.config, self.tools, self.llm_client,
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
            'test_gen_result': None,
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
            _print_step_header(1, 6, '提取异常堆栈')
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
            logger.info('  异常类型: %s', exc_type)
            logger.info('  顶层帧:   %s', top_frame)
            logger.info('────────────────────────────────────────────────────────────')

            # ── Stage 2: Agent 决策循环 ──
            _print_step_header(2, 6, 'Agent 决策循环')
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
            logger.info('────────────────────────────────────────────────────────────')

            # ── Stage 3: 补丁校验 ──
            _print_step_header(3, 6, '补丁校验')
            validation = self.validate_patch(repo_path, patch_text, source_info=source_info)
            if not validation['valid']:
                raise ValueError('Patch validation failed: %s' % '; '.join(validation['errors']))
            logger.info('  ✅ 校验通过')
            logger.info('────────────────────────────────────────────────────────────')

            branch_name = self._make_branch_name()
            report['branch_name'] = branch_name

            if not dry_run:
                # ── Stage 4: 应用补丁 ──
                _print_step_header(4, 6, '应用补丁')
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
                    logger.info('  ✅ 已提交: %s (%d 文件)',
                                commit_msg, len(apply_result.get('files', [])))
                    logger.info('────────────────────────────────────────────────────────────')

                if apply_result.get('applied'):
                    # ── Stage 5: 自动生成测试 ──
                    if self.config.get('generate_tests', True):
                        _print_step_header(5, 6, '生成 JUnit 测试')

                        # 优先使用 agent loop 中生成的测试代码
                        agent_test_code = agent_result.get('test_code', '')
                        if agent_test_code:
                            logger.info('  使用 Agent 生成的测试代码')
                            test_rel_path = prompt_builder._derive_test_path(source_info)
                            written_path = test_generator.write_test_file(
                                repo_path, test_rel_path, agent_test_code
                            )
                            if written_path:
                                commit_msg = 'test: add JUnit tests for auto-fix in %s' % (
                                    source_info.get('class_name', 'unknown')
                                )
                                committed = self.tools['git_manager'].commit_changes(
                                    repo_path, commit_msg, files=[test_rel_path]
                                )
                                test_gen_result = {
                                    'generated': True,
                                    'test_path': test_rel_path,
                                    'committed': committed,
                                    'error': None,
                                }
                            else:
                                test_gen_result = {
                                    'generated': False,
                                    'test_path': '',
                                    'committed': False,
                                    'error': 'Failed to write test file',
                                }
                        else:
                            # Fallback: 使用独立的 test_generator
                            logger.info('  Agent 未生成测试，使用独立生成器')
                            test_gen_result = test_generator.run_test_generation(
                                self.config, repo_path, source_info,
                                report['patch_text'], raw_stack,
                                self.llm_client, self.tools,
                            )

                        report['test_gen_result'] = test_gen_result
                        if test_gen_result.get('generated'):
                            logger.info('  ✅ 测试已生成: %s', test_gen_result.get('test_path', ''))
                        else:
                            logger.warning('  ❌ 测试生成失败: %s', test_gen_result.get('error', 'unknown'))
                        logger.info('────────────────────────────────────────────────────────────')

                    # ── Stage 6: CI 管道 ──
                    _print_step_header(6, 6, 'CI 管道')
                    ci_result = self._run_ci_pipeline(
                        repo_path, source_info, raw_stack, parsed_stack,
                        report['patch_text'], report['prompt'],
                        initial_files=apply_result.get('files', []),
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
                            logger.info('  ✅ PR: %s', pr_result.get('pr_url', ''))
                    logger.info('────────────────────────────────────────────────────────────')
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

    def find_source_location(self, repo_path, frame, repo_graph=None):
        return source_locator.find_source_location(repo_path, frame, self.tools['file_io'], repo_graph)

    def _locate_file_by_class_or_path(self, repo_path, class_name, file_path):
        return source_locator.locate_file_by_class_or_path(repo_path, class_name, file_path)

    def _find_method_line(self, lines, method_name):
        return source_locator.find_method_line(lines, method_name)

    # ── Delegation: prompt building ──────────────────────────────────

    def build_prompt(self, raw_stack, source_info):
        return prompt_builder.build_prompt(self.config, raw_stack, source_info)

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
