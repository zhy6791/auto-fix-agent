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
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
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
    
    logger.info(f"Loaded config from {config_path}")
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
        logger.info("command_whitelist not set; using built-in default whitelist")
    elif not isinstance(command_whitelist, list) or not all(isinstance(item, str) and item.strip() for item in command_whitelist):
        raise ValueError('command_whitelist must be a non-empty list of command names')
    
    logger.info("Config validation passed")


def print_report(report, output_file=None):
    """Pretty-print the agent report."""
    print("\n" + "="*70)
    print("AUTOFIX AGENT REPORT")
    print("="*70)

    print(f"\nStatus: {report.get('status')}")
    print(f"Dry Run: {report.get('dry_run')}")

    if report.get('error'):
        print(f"\n[ERROR] {report['error']}")
        return

    if report.get('parsed_stack'):
        print(f"\nParsed Frames: {len(report['parsed_stack'])}")
        for idx, frame in enumerate(report['parsed_stack'][:3]):
            print(f"  [{idx}] {frame.get('class_name')}.{frame.get('method')}() "
                  f"@ line {frame.get('line_no')}")

    if report.get('located_files'):
        print(f"\nLocated Files:")
        for info in report['located_files']:
            print(f"  - {info.get('repo_relative_path')} (line {info.get('line_no')})")

    if report.get('branch_name'):
        print(f"\nBranch Name: {report['branch_name']}")

    if report.get('patch_text'):
        patch_preview = report['patch_text'][:200]
        if len(report['patch_text']) > 200:
            patch_preview += "..."
        print(f"\nPatch Preview:\n{patch_preview}")

    apply_result = report.get('apply_result', {})
    if apply_result.get('applied'):
        print(f"\n[OK] Patch Applied!")
        print(f"   Files touched: {', '.join(apply_result.get('files', []))}")
    elif not apply_result.get('dry_run'):
        print(f"\n[WARN] Patch Not Applied")
        if apply_result.get('errors'):
            for err in apply_result['errors']:
                print(f"   Error: {err}")

    if report.get('build_result'):
        build = report['build_result']
        print(f"\nBuild Result:")
        print(f"   Exit code: {build.get('code')}")
        if build.get('code') == 0:
            print(f"   [OK] Build successful")
        else:
            print(f"   [FAIL] Build failed")

    ci_result = report.get('ci_result', {})
    if ci_result:
        print(f"\nCI Pipeline:")
        for stage in ci_result.get('stages_passed', []):
            print(f"   [OK] {stage}")
        for stage in ci_result.get('stages_failed', []):
            print(f"   [FAIL] {stage}")
        if ci_result.get('retries_used', 0) > 0:
            print(f"   Retries used: {ci_result['retries_used']}")

    pr_result = report.get('pr_result', {})
    if pr_result:
        if pr_result.get('pr_created'):
            print(f"\n[OK] Pull Request created: {pr_result['pr_url']}")
        elif pr_result.get('error'):
            print(f"\n[WARN] PR creation failed: {pr_result['error']}")

    print("\n" + "="*70)

    if output_file:
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        logger.info(f"Full report saved to {output_file}")


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
        logger.info("Loading configuration...")
        config = load_config(args.config)
        
        logger.info("Validating configuration...")
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
        logger.info(f"Starting agent pipeline (dry_run={dry_run})...")
        
        agent = AutoFixAgent(
            config,
            tools={'file_io': file_io, 'exec_cmd': exec_cmd, 'git_manager': git_manager}
        )
        
        report = agent.run_pipeline(dry_run=dry_run)
        
        output_file = f"agent_report_{report.get('branch_name', 'unknown')}.json" if report.get('branch_name') else None
        print_report(report, output_file=output_file)
        
        if report.get('status') == 'completed':
            logger.info("Pipeline completed successfully")
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
