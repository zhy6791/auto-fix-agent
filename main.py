"""CLI entry point for AutoFixAgent demo.

Usage:
    python -m main --config configs/config.yml --dry-run
    python -m main --config configs/config.yml --auto-apply
"""

import argparse
import sys
import os
import json
import logging

import yaml

from agents.auto_fix_agent import AutoFixAgent
from tools import file_io, exec_cmd, git_manager


logging.basicConfig(
    level=logging.INFO,
    format='%(message)s',
)
logger = logging.getLogger(__name__)


DEFAULT_COMMAND_WHITELIST = [
    'mvnw.cmd', 'mvnw', 'mvn.cmd', 'mvn.bat', 'mvn',
    'gradlew.bat', 'gradlew', 'gradle.bat', 'gradle.cmd', 'gradle',
]


def load_config(config_path):
    """Load YAML configuration from file."""
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f) or {}

    return config


def validate_config(config):
    """Validate required configuration fields."""
    required_fields = ['logs_path', 'repo_path']
    missing = [f for f in required_fields if not config.get(f)]
    if missing:
        raise ValueError(f"Missing required config fields: {', '.join(missing)}")

    if not os.path.exists(config.get('logs_path', '')):
        raise FileNotFoundError(f"Logs file not found: {config['logs_path']}")

    if not os.path.exists(config.get('repo_path', '')):
        raise FileNotFoundError(f"Repo path not found: {config['repo_path']}")

    command_whitelist = config.get('command_whitelist')
    if not command_whitelist:
        config['command_whitelist'] = list(DEFAULT_COMMAND_WHITELIST)
    elif not isinstance(command_whitelist, list) or not all(isinstance(item, str) and item.strip() for item in command_whitelist):
        raise ValueError('command_whitelist must be a non-empty list of command names')


def _ok(k):
    return '\033[32m✔\033[0m' if k else '\033[31m✘\033[0m'


def print_report(report, output_file=None):
    """Print a concise agent report."""
    status = report.get('status', 'unknown')
    dry_run = report.get('dry_run', False)
    error = report.get('error')
    parsed = report.get('parsed_stack', [])
    located = report.get('located_files', [])
    patch_text = report.get('patch_text', '')
    apply_result = report.get('apply_result', {})
    ci_result = report.get('ci_result', {})
    pr_result = report.get('pr_result', {})
    iterations = report.get('agent_iterations', 0)
    tool_calls = report.get('agent_tool_calls', [])

    # ── Report 标题 ──
    print()
    print('\033[36m════════════════════════════════════════════════════════════\033[0m')
    print('\033[1;36m  📋 执行报告\033[0m')
    print('\033[36m════════════════════════════════════════════════════════════\033[0m')

    if error:
        print(f'\n  \033[31m✘ 执行失败: {error}\033[0m\n')
        print('\033[36m════════════════════════════════════════════════════════════\033[0m\n')
        _save_report(report, output_file)
        return

    # ── 异常信息 ──
    if parsed:
        exc = parsed[0].get('exception_type', 'Exception')
        top = '%s.%s():%s' % (parsed[0].get('class_name', ''), parsed[0].get('method', ''), parsed[0].get('line_no', '?'))
        print(f'\n  \033[1m异常类型:\033[0m \033[33m{exc}\033[0m')
        print(f'  \033[1m触发位置:\033[0m {top}')

    # ── 定位结果 ──
    if located:
        info = located[0]
        detection = info.get('detection_method', 'agent')
        print(f'  \033[1m定位方式:\033[0m {detection}')
        print(f'  \033[1m源文件:\033[0m   {info.get("repo_relative_path", "?")}:{info.get("line_no", "?")}')

    # ── 补丁内容 ──
    if patch_text:
        print(f'\n  \033[1m补丁内容:\033[0m')
        for line in patch_text.splitlines():
            if line.startswith('@@') or line.startswith('---') or line.startswith('+++'):
                print(f'  \033[36m{line}\033[0m')
            elif line.startswith('+') and not line.startswith('+++'):
                print(f'  \033[32m{line}\033[0m')
            elif line.startswith('-') and not line.startswith('---'):
                print(f'  \033[31m{line}\033[0m')
            elif line.strip():
                print(f'  \033[90m{line}\033[0m')

    # ── 执行结果 ──
    print(f'\n  \033[1m执行结果:\033[0m')
    applied = apply_result.get('applied', False)
    branch = report.get('branch_name', '')
    files = apply_result.get('files', [])
    if applied:
        print(f'  {_ok(True)} 补丁已应用  \033[90m分支: {branch}\033[0m  \033[90m文件: {", ".join(files)}\033[0m')
    elif dry_run:
        print(f'  \033[33m○\033[0m dry-run 模式，未修改代码')
    else:
        errs = '; '.join(apply_result.get('errors', []))
        print(f'  {_ok(False)} 补丁未应用  \033[31m{errs}\033[0m')

    # ── CI 结果 ──
    if ci_result:
        passed = ci_result.get('stages_passed', [])
        failed = ci_result.get('stages_failed', [])
        retries = ci_result.get('retries_used', 0)
        print(f'\n  \033[1mCI 管道:\033[0m')
        parts = []
        for s in passed:
            parts.append(f'{_ok(True)} {s}')
        for s in failed:
            parts.append(f'{_ok(False)} {s}')
        line = '  ' + '  '.join(parts)
        if retries > 0:
            line += f'  \033[33m↻ LLM重试 {retries} 次\033[0m'
        print(line)
        # Show last error detail for failed stages
        for entry in reversed(ci_result.get('patch_history', [])):
            if entry.get('stage') in failed and entry.get('error'):
                err_preview = entry['error'].replace('\n', ' ').strip()[:150]
                if err_preview:
                    print(f'  \033[90m└ 错误详情: {err_preview}\033[0m')
                break

    # ── PR 结果 ──
    if pr_result:
        print(f'\n  \033[1mPull Request:\033[0m')
        if pr_result.get('pr_created'):
            print(f'  {_ok(True)} PR 已创建: \033[4m{pr_result["pr_url"]}\033[0m')
        elif pr_result.get('error'):
            print(f'  {_ok(False)} PR 创建失败: \033[31m{pr_result["error"]}\033[0m')

    # ── 统计摘要 ──
    print(f'\n  \033[1m统计摘要:\033[0m')
    stats = []
    if iterations > 0:
        stats.append(f'Agent 迭代 {iterations} 轮')
    if tool_calls:
        stats.append(f'工具调用 {len(tool_calls)} 次')
    stats.append('模式: dry-run' if dry_run else f'状态: {status}')
    print(f'  \033[90m{" · ".join(stats)}\033[0m')
    print()
    print('\033[36m════════════════════════════════════════════════════════════\033[0m\n')

    _save_report(report, output_file)


def _save_report(report, output_file):
    if output_file:
        os.makedirs(os.path.dirname(output_file) or '.', exist_ok=True)
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        logger.info(f"Report saved to {output_file}")


def main():
    parser = argparse.ArgumentParser(
        description='Java Web Service AutoFix Agent Demo',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  python -m main --config configs/config.yml
  python -m main --config configs/config.yml --dry-run
  python -m main --config configs/config.yml --auto-apply
        '''
    )
    
    parser.add_argument(
        '--config',
        type=str,
        default='configs/config.yml',
        help='Path to configuration file (default: configs/config.yml)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        default=False,
        help='Run in dry-run mode (analyze only, no changes to repo)'
    )
    parser.add_argument(
        '--auto-apply',
        action='store_true',
        help='Automatically apply patch to working tree (creates branch, modifies files, but no commit)'
    )
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Enable verbose logging'
    )
    parser.add_argument(
        '--no-compile',
        action='store_true',
        help='Skip compile check even if run_compile_on_apply is set in config'
    )
    parser.add_argument(
        '--no-tests',
        action='store_true',
        help='Skip test run even if run_tests_on_apply is set in config'
    )
    parser.add_argument(
        '--create-pr',
        action='store_true',
        help='Enable Gitee PR creation (overrides gitee.enabled in config)'
    )
    parser.add_argument(
        '--max-retries',
        type=int,
        default=None,
        help='Maximum retry attempts on compile/test failure (default: from config)'
    )
    parser.add_argument(
        '--max-agent-iterations',
        type=int,
        default=None,
        help='Maximum iterations for the agent decision loop (default: 10)'
    )

    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    try:
        config = load_config(args.config)
        validate_config(config)

        if args.no_compile:
            config['run_compile_on_apply'] = False
        if args.no_tests:
            config['run_tests_on_apply'] = False
        if args.create_pr:
            config.setdefault('gitee', {})
            config['gitee']['enabled'] = True
        if args.max_retries is not None:
            config['max_retries'] = args.max_retries
        if args.max_agent_iterations is not None:
            config['max_agent_iterations'] = args.max_agent_iterations

        dry_run = args.dry_run or not args.auto_apply

        print()
        print('\033[36m  ╔══════════════════════════════════════════════════════════╗\033[0m')
        print('\033[36m  ║\033[0m  \033[1;36m⚡ AutoFix Agent\033[0m  ·  \033[90mJava Web 服务自动修复\033[0m                 \033[36m║\033[0m')
        print('\033[36m  ║\033[0m  \033[90mLLM 驱动异常分析 → 源码定位 → 补丁生成 → CI → PR\033[0m   \033[36m║\033[0m')
        print('\033[36m  ╚══════════════════════════════════════════════════════════╝\033[0m')
        print()
        logger.info('')
        logger.info('  日志: %s', config.get('logs_path', ''))
        logger.info('  仓库: %s', config.get('repo_path', ''))
        logger.info('  模式: %s', 'dry-run' if dry_run else 'auto-apply')

        agent = AutoFixAgent(
            config,
            tools={'file_io': file_io, 'exec_cmd': exec_cmd, 'git_manager': git_manager}
        )

        report = agent.run_pipeline(dry_run=dry_run)

        output_file = f"agent_report_{report.get('branch_name', 'unknown')}.json" if report.get('branch_name') else None
        print_report(report, output_file=output_file)

        if report.get('status') == 'completed':
            return 0
        else:
            logger.error("Pipeline failed: %s", report.get('error'))
            return 1

    except FileNotFoundError as e:
        logger.error("File error: %s", e)
        return 1
    except ValueError as e:
        logger.error("Validation error: %s", e)
        return 1
    except Exception as e:
        logger.exception("Unexpected error: %s", e)
        return 1


if __name__ == '__main__':
    sys.exit(main())
