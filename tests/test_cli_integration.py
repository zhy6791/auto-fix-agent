"""End-to-end integration test for CLI and agent pipeline."""

import os
import sys
import subprocess
import shutil
import tempfile
import unittest

import yaml


class TestCLIIntegration(unittest.TestCase):
    
    def setUp(self):
        self.workdir = tempfile.mkdtemp()
        self.repo_path = os.path.join(self.workdir, 'test-repo')
        self.log_path = os.path.join(self.workdir, 'app.log')
        self.config_path = os.path.join(self.workdir, 'config.yml')
        
        os.makedirs(self.repo_path)
        subprocess.check_call(['git', 'init'], cwd=self.repo_path)
        subprocess.check_call(['git', 'config', 'user.name', 'Test'], cwd=self.repo_path)
        subprocess.check_call(['git', 'config', 'user.email', 'test@test.com'], cwd=self.repo_path)
        
        source_dir = os.path.join(self.repo_path, 'src', 'main', 'java', 'com', 'example')
        os.makedirs(source_dir)
        source_file = os.path.join(source_dir, 'TestClass.java')
        with open(source_file, 'w') as f:
            f.write('\n'.join([
                'package com.example;',
                'public class TestClass {',
                '    public String process(String input) {',
                '        return input.length() > 0 ? input : null;',
                '    }',
                '}'
            ]))
        
        subprocess.check_call(['git', 'add', '.'], cwd=self.repo_path)
        subprocess.check_call(['git', 'commit', '-m', 'Initial'], cwd=self.repo_path)
        
        log_content = '\n'.join([
            '2026-05-07 15:00:00 ERROR',
            'java.lang.NullPointerException: Cannot invoke "java.lang.String.length()" because "input" is null',
            '    at com.example.TestClass.process(TestClass.java:4)',
            '    at java.lang.Thread.run(Thread.java:745)',
            '',
            '2026-05-07 15:01:00 INFO Application continue',
        ])
        with open(self.log_path, 'w') as f:
            f.write(log_content)
        
        config = {
            'logs_path': self.log_path,
            'repo_path': self.repo_path,
            'java_build': 'maven',
            'branch_prefix': 'fix/',
            'llm': {
                'provider': 'openai',
                'model': 'gpt-4o-mini',
                'api_key_env': 'DUMMY_KEY',
                'temperature': 0.2,
            },
            'max_patch_lines': 40,
            'command_whitelist': [
                'mvnw.cmd', 'mvnw', 'mvn.cmd', 'mvn.bat', 'mvn',
                'gradlew.bat', 'gradlew', 'gradle.bat', 'gradle.cmd', 'gradle',
            ],
            'auto_apply': False,
            'run_tests_on_apply': False,
        }
        with open(self.config_path, 'w') as f:
            yaml.dump(config, f)
    
    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)
    
    def test_config_loading(self):
        """Test that config file can be loaded and validated."""
        with open(self.config_path) as f:
            config = yaml.safe_load(f)
        
        self.assertIsNotNone(config)
        self.assertEqual(config['logs_path'], self.log_path)
        self.assertEqual(config['repo_path'], self.repo_path)
    
    def test_cli_help(self):
        """Test that CLI --help works."""
        result = subprocess.run(
            [sys.executable, 'main.py', '--help'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        
        self.assertEqual(result.returncode, 0)
        self.assertIn('--config', result.stdout)
        self.assertIn('--dry-run', result.stdout)
        self.assertIn('--auto-apply', result.stdout)
    
    def test_cli_config_not_found(self):
        """Test that CLI fails gracefully when config not found."""
        result = subprocess.run(
            [sys.executable, 'main.py', '--config', '/nonexistent/config.yml'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Config file not found', result.stderr)
    
    def test_cli_missing_repo_path(self):
        """Test that CLI fails when repo_path doesn't exist."""
        bad_config = os.path.join(self.workdir, 'bad_config.yml')
        config = {
            'logs_path': self.log_path,
            'repo_path': '/nonexistent/repo',
        }
        with open(bad_config, 'w') as f:
            yaml.dump(config, f)
        
        result = subprocess.run(
            [sys.executable, 'main.py', '--config', bad_config],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not found', result.stderr)


    def test_cli_help_includes_new_flags(self):
        """Test that CLI --help shows new flags."""
        result = subprocess.run(
            [sys.executable, 'main.py', '--help'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn('--no-compile', result.stdout)
        self.assertIn('--no-tests', result.stdout)
        self.assertIn('--create-pr', result.stdout)
        self.assertIn('--max-retries', result.stdout)

    def test_cli_no_compile_flag(self):
        """Test that --no-compile overrides config."""
        os.environ['DUMMY_KEY'] = 'test-key'
        try:
            # Run dry-run with --no-compile to verify flag is accepted
            result = subprocess.run(
                [sys.executable, 'main.py', '--config', self.config_path,
                 '--dry-run', '--no-compile', '--max-agent-iterations', '1'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=15,
            )
            # Should complete (or fail for other reasons) without flag-related error
            self.assertNotIn('unrecognized arguments', result.stderr)
        finally:
            del os.environ['DUMMY_KEY']

    def test_cli_max_retries_flag(self):
        """Test that --max-retries flag is accepted."""
        os.environ['DUMMY_KEY'] = 'test-key'
        try:
            result = subprocess.run(
                [sys.executable, 'main.py', '--config', self.config_path,
                 '--dry-run', '--max-retries', '5', '--max-agent-iterations', '1'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=15,
            )
            self.assertNotIn('unrecognized arguments', result.stderr)
        finally:
            del os.environ['DUMMY_KEY']

    def test_cli_max_agent_iterations_flag(self):
        """Test that --max-agent-iterations flag is accepted."""
        os.environ['DUMMY_KEY'] = 'test-key'
        try:
            result = subprocess.run(
                [sys.executable, 'main.py', '--config', self.config_path,
                 '--dry-run', '--max-agent-iterations', '2'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=15,
            )
            self.assertNotIn('unrecognized arguments', result.stderr)
        finally:
            del os.environ['DUMMY_KEY']


if __name__ == '__main__':
    unittest.main()



