"""CI pipeline: compile, test, retry with LLM feedback."""

import logging
import os
import shutil

logger = logging.getLogger(__name__)


def resolve_build_cmd(repo_path, build_tool, *args):
    """Return [executable, ...args] preferring project wrapper scripts."""
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


def is_tool_missing(build_result):
    """Return True if the build failed because the tool itself is missing."""
    stderr = (build_result.get('stderr', '') or '').lower()
    return 'command not found' in stderr or 'filenotfounderror' in stderr.replace(' ', '')


def run_compile(repo_path, config, exec_cmd):
    """Run compile-only check (no tests)."""
    build_tool = config.get('java_build', 'maven')
    allowed_commands = config.get('command_whitelist', [])
    if build_tool == 'gradle':
        cmd = resolve_build_cmd(repo_path, 'gradle', 'compileJava')
    else:
        cmd = resolve_build_cmd(repo_path, 'maven', 'compile', '-q')
    return exec_cmd.run(cmd, cwd=repo_path, timeout=600, allowed_commands=allowed_commands)


def run_tests(repo_path, config, exec_cmd):
    """Run unit tests."""
    build_tool = config.get('java_build', 'maven')
    allowed_commands = config.get('command_whitelist', [])
    if build_tool == 'gradle':
        cmd = resolve_build_cmd(repo_path, 'gradle', 'test')
    else:
        cmd = resolve_build_cmd(repo_path, 'maven', 'test', '-q')
    return exec_cmd.run(cmd, cwd=repo_path, timeout=1200, allowed_commands=allowed_commands)


def revert_files(repo_path, files, git_manager, ref='HEAD'):
    """Revert specified files to a given git ref."""
    return git_manager.revert_files(repo_path, files, ref)


def retry_with_feedback(original_prompt, source_info, failed_stage,
                        build_result, repo_path, config, llm_client, validate_fn):
    """Feed build/test failure back to LLM for a corrected patch.

    Returns new patch text, or None if LLM cannot fix.
    """
    from agents.agent_loop import prompt_builder
    from agents.post_processing import patch_validator

    error_output = (build_result.get('stderr', '') or '')[:3000]
    if not error_output:
        error_output = (build_result.get('stdout', '') or '')[:3000]

    # For test failures, include the broken test file content so LLM can fix it
    broken_file_content = None
    if failed_stage == 'tests':
        from agents.agent_loop.prompt_builder import _derive_test_path
        test_path = _derive_test_path(source_info)
        abs_test = os.path.join(repo_path, test_path)
        if os.path.exists(abs_test):
            try:
                with open(abs_test, 'r', encoding='utf-8') as f:
                    broken_file_content = f.read()
            except Exception:
                pass

    retry_prompt = prompt_builder.build_retry_prompt(
        original_prompt, source_info, failed_stage, error_output,
        broken_file_content=broken_file_content,
    )

    max_tokens = int(config.get('max_tokens', 8192))
    new_patch_text = llm_client.generate_patch(retry_prompt, max_tokens=max_tokens)

    if patch_validator.is_no_safe_patch(new_patch_text):
        return None

    validation = validate_fn(repo_path, new_patch_text, source_info=source_info)
    if not validation['valid']:
        logger.warning('Retry patch failed validation: %s', '; '.join(validation['errors']))
        return None

    return new_patch_text


def run_ci_pipeline(repo_path, source_info, raw_stack, parsed_stack,
                    original_patch_text, original_prompt,
                    config, tools, llm_client, validate_fn,
                    initial_files=None, compile_fn=None, test_fn=None):
    """Run compile then tests, with LLM retry on failure.

    compile_fn and test_fn allow injecting custom build/test functions (for testing).
    Defaults to run_compile / run_tests from this module.

    Returns dict: {compile_result, test_result, retries_used, patch_history,
                   stages_passed, stages_failed}
    """
    if compile_fn is None:
        compile_fn = lambda rp: run_compile(rp, config, tools['exec_cmd'])
    if test_fn is None:
        test_fn = lambda rp: run_tests(rp, config, tools['exec_cmd'])

    max_retries = int(config.get('max_retries', 3))
    run_compile_flag = config.get('run_compile_on_apply', False)
    run_tests_flag = config.get('run_tests_on_apply', False)

    ci_result = {
        'compile_result': None,
        'test_result': None,
        'retries_used': 0,
        'patch_history': [],
        'stages_passed': [],
        'stages_failed': [],
    }

    if not run_compile_flag and not run_tests_flag:
        return ci_result

    current_patch = original_patch_text
    current_prompt = original_prompt
    retry_files = []
    first_retry = True

    # Stage 1: Compile with retry
    if run_compile_flag:
        for attempt in range(max_retries + 1):
            compile_result = compile_fn(repo_path)
            if compile_result['code'] == 0:
                ci_result['compile_result'] = compile_result
                ci_result['stages_passed'].append('compile')
                break

            # System-level failures cannot be fixed by LLM
            if is_tool_missing(compile_result):
                ci_result['compile_result'] = compile_result
                ci_result['stages_failed'].append('compile')
                ci_result['patch_history'].append({
                    'stage': 'compile',
                    'attempt': attempt + 1,
                    'error': 'Build tool not installed: %s'
                             % (compile_result.get('stderr', '') or '').strip(),
                })
                break

            err = (compile_result.get('stderr', '') or '')[:2000]
            if not err:
                err = (compile_result.get('stdout', '') or '')[:2000]
            ci_result['patch_history'].append({
                'stage': 'compile',
                'attempt': attempt + 1,
                'error': err,
            })
            if attempt >= max_retries:
                ci_result['compile_result'] = compile_result
                ci_result['stages_failed'].append('compile')
                break

            # Only count actual LLM retries (not the first failed attempt)
            ci_result['retries_used'] += 1
            new_patch = retry_with_feedback(
                current_prompt, source_info, 'compile',
                compile_result, repo_path, config, llm_client, validate_fn
            )
            if new_patch is None:
                ci_result['stages_failed'].append('compile')
                break
            # Revert previously patched files before applying retry
            revert_target = initial_files if first_retry else retry_files
            if revert_target:
                revert_files(repo_path, revert_target, tools['git_manager'], ref='HEAD~1')
            first_retry = False
            apply_result = tools['git_manager'].apply_patch(repo_path, new_patch)
            retry_files = apply_result.get('files', [])
            current_patch = new_patch

    # Stage 2: Tests (only if compile passed)
    if run_tests_flag and 'compile' not in ci_result['stages_failed']:
        for attempt in range(max_retries + 1):
            test_result = test_fn(repo_path)
            if test_result['code'] == 0:
                ci_result['test_result'] = test_result
                ci_result['stages_passed'].append('tests')
                break

            if is_tool_missing(test_result):
                ci_result['test_result'] = test_result
                ci_result['stages_failed'].append('tests')
                ci_result['patch_history'].append({
                    'stage': 'tests',
                    'attempt': attempt + 1,
                    'error': 'Test tool not installed: %s'
                             % (test_result.get('stderr', '') or '').strip(),
                })
                break

            err = (test_result.get('stderr', '') or '')[:2000]
            if not err:
                err = (test_result.get('stdout', '') or '')[:2000]
            ci_result['patch_history'].append({
                'stage': 'tests',
                'attempt': attempt + 1,
                'error': err,
            })
            if attempt >= max_retries:
                ci_result['test_result'] = test_result
                ci_result['stages_failed'].append('tests')
                break

            ci_result['retries_used'] += 1
            new_patch = retry_with_feedback(
                current_prompt, source_info, 'tests',
                test_result, repo_path, config, llm_client, validate_fn
            )
            if new_patch is None:
                ci_result['stages_failed'].append('tests')
                break
            # Revert previously patched files before applying retry
            if retry_files:
                revert_files(repo_path, retry_files, tools['git_manager'], ref='HEAD~1')
            apply_result = tools['git_manager'].apply_patch(repo_path, new_patch)
            retry_files = apply_result.get('files', [])
            current_patch = new_patch

    if ci_result['retries_used'] > 0 and current_patch != original_patch_text and retry_files:
        tools['git_manager'].commit_changes(
            repo_path,
            'fix: retry patch (%d LLM retries)' % ci_result['retries_used'],
            files=list(set(retry_files))
        )

    return ci_result
