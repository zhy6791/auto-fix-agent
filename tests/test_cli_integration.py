"""End-to-end integration test for CLI and agent pipeline."""

import os
import sys
import subprocess
import json
import shutil
import tempfile
import subprocess
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


if __name__ == '__main__':
    unittest.main()



