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
