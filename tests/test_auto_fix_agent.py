"""Unit tests for AutoFixAgent Phase 3 pipeline."""

import os
import json
import shutil
import subprocess
import tempfile
import unittest

from agents.auto_fix_agent import AutoFixAgent
from tools import file_io, exec_cmd, git_manager


class MockLLMClient(object):
    def __init__(self, patch_text):
        self.patch_text = patch_text
        self.last_prompt = None

    def generate_patch(self, prompt, max_tokens=1024):
        self.last_prompt = prompt
        return self.patch_text


class TestAutoFixAgent(unittest.TestCase):

    def setUp(self):
        self.workdir = tempfile.mkdtemp()
        self.repo_path = os.path.join(self.workdir, 'mall-service')
        os.makedirs(self.repo_path)

        subprocess.check_call(['git', 'init'], cwd=self.repo_path)
        subprocess.check_call(['git', 'config', 'user.name', 'Test User'], cwd=self.repo_path)
        subprocess.check_call(['git', 'config', 'user.email', 'test@example.com'], cwd=self.repo_path)

        # Create an initial commit so branch checkout works normally.
        readme_path = os.path.join(self.repo_path, 'README.md')
        file_io.write_file(readme_path, '# mall-service\n', overwrite=True)
        subprocess.check_call(['git', 'add', 'README.md'], cwd=self.repo_path)
        subprocess.check_call(['git', 'commit', '-m', 'Initial commit'], cwd=self.repo_path)

        self.source_rel = os.path.join('src', 'main', 'java', 'com', 'example', 'demo', 'controller', 'HelloController.java')
        self.source_abs = os.path.join(self.repo_path, self.source_rel)
        os.makedirs(os.path.dirname(self.source_abs))

        # Build a file with enough lines so line 42 exists.
        source_lines = []
        source_lines.append('package com.example.demo.controller;')
        source_lines.append('')
        source_lines.append('public class HelloController {')
        for i in range(4, 41):
            source_lines.append('    // filler line %d' % i)
        source_lines.append('    public int sayHello(String str) {')  # line 42-ish depending on previous lines
        source_lines.append('        return str.length();')
        source_lines.append('    }')
        source_lines.append('}')
        # pad to keep line numbers predictable
        while len(source_lines) < 50:
            source_lines.append('    // tail filler %d' % len(source_lines))

        file_io.write_file(self.source_abs, '\n'.join(source_lines), overwrite=True)

        self.log_path = os.path.join(self.workdir, 'app.log')
        log_text = '\n'.join([
            '2026-05-07 15:03:12,345 ERROR [http-nio-8080-exec-5] Servlet.service() threw exception',
            'java.lang.NullPointerException: Cannot invoke "java.lang.String.length()" because "str" is null',
            '\tat com.example.demo.controller.HelloController.sayHello(HelloController.java:42)',
            '\tat org.springframework.web.servlet.FrameworkServlet.processRequest(FrameworkServlet.java:1006)',
            '',
            '2026-05-07 15:10:01,111 INFO  Application started successfully',
        ])
        file_io.write_file(self.log_path, log_text, overwrite=True)

        self.config = {
            'logs_path': self.log_path,
            'repo_path': self.repo_path,
            'java_build': 'maven',
            'branch_prefix': 'fix/',
            'llm': {
                'provider': 'openai',
                'model': 'mimo-v2.5-pro',
                'api_key_env': 'dummy-key',
                'temperature': 0.2,
            },
            'max_patch_lines': 40,
            'auto_apply': False,
            'run_tests_on_apply': False,
        }

        self.original_source = file_io.read_file(self.source_abs)
        patched_source = self.original_source.replace('        return str.length();', '        return str == null ? 0 : str.length();')
        self.patched_source = patched_source
        self.mock_llm = MockLLMClient(json.dumps({
            'files': [
                {
                    'path': self.source_rel.replace('\\', '/'),
                    'patched_content': self.patched_source,
                }
            ]
        }))

        self.agent = AutoFixAgent(
            self.config,
            tools={'file_io': file_io, 'exec_cmd': exec_cmd, 'git_manager': git_manager},
            llm_client=self.mock_llm,
        )

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_parse_stacktrace(self):
        stack = self.agent.extract_latest_exception_block(file_io.read_file(self.log_path))
        parsed = self.agent.parse_stacktrace(stack)

        self.assertTrue(parsed)
        self.assertEqual(parsed[0]['exception_type'], 'java.lang.NullPointerException')
        self.assertEqual(parsed[0]['class_name'], 'com.example.demo.controller.HelloController')
        self.assertEqual(parsed[0]['method'], 'sayHello')
        self.assertEqual(parsed[0]['line_no'], 42)

    def test_find_source_location(self):
        frame = {
            'class_name': 'com.example.demo.controller.HelloController',
            'method': 'sayHello',
            'line_no': 42,
        }
        info = self.agent.find_source_location(self.repo_path, frame)

        self.assertEqual(os.path.normpath(info['source_path']), os.path.normpath(self.source_abs))
        self.assertEqual(info['repo_relative_path'].replace('\\', '/'), self.source_rel.replace('\\', '/'))
        self.assertIn('42:', info['context_snippet'])

    def test_run_pipeline_dry_run(self):
        report = self.agent.run_pipeline(dry_run=True)

        self.assertEqual(report['status'], 'completed')
        self.assertTrue(report['parsed_stack'])
        self.assertTrue(report['located_files'])
        self.assertTrue(report['branch_name'].startswith('fix/'))
        self.assertTrue(report['apply_result']['dry_run'])
        self.assertFalse(report['apply_result']['applied'])
        self.assertEqual(file_io.read_file(self.source_abs), self.original_source)

    def test_run_pipeline_applies_patch(self):
        report = self.agent.run_pipeline(dry_run=False)

        self.assertEqual(report['status'], 'completed')
        self.assertTrue(report['branch_name'].startswith('fix/'))
        self.assertTrue(report['apply_result']['applied'])
        self.assertIn(self.source_rel.replace('\\', '/'), [p.replace('\\', '/') for p in report['apply_result']['files']])
        self.assertEqual(file_io.read_file(self.source_abs), self.patched_source)

        current_branch = subprocess.check_output(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            cwd=self.repo_path,
            universal_newlines=True,
        ).strip()
        self.assertTrue(current_branch.startswith('fix/'))


if __name__ == '__main__':
    unittest.main()



