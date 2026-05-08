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
        # Check that context snippet contains the code (no line number prefix)
        self.assertIn('sayHello', info['context_snippet'])

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

    def test_validate_patch_rejects_non_local_java_changes(self):
        suspicious_source = self.original_source.replace(
            'package com.example.demo.controller;',
            'package com.example.demo.service;'
        ).replace(
            '        return str.length();',
            '        return str == null ? 0 : str.length();'
        )
        patch_text = json.dumps({
            'files': [
                {
                    'path': self.source_rel.replace('\\', '/'),
                    'patched_content': suspicious_source,
                }
            ]
        })

        result = self.agent.validate_patch(self.repo_path, patch_text, source_info={
            'repo_relative_path': self.source_rel.replace('\\', '/'),
            'line_no': 42,
        })

        self.assertFalse(result['valid'])
        # New validation should catch package change
        self.assertTrue(any('Package declaration was modified' in e or 'Package' in e for e in result['errors']))

    def test_validate_patch_rejects_method_deletion_outside_window(self):
        # create an extra file with two methods, then produce a patch that deletes one method far from the analyzed line
        extra_rel = os.path.join('src', 'main', 'java', 'com', 'example', 'demo', 'controller', 'Helper.java')
        extra_abs = os.path.join(self.repo_path, extra_rel)
        os.makedirs(os.path.dirname(extra_abs), exist_ok=True)
        old_content_lines = [
            'package com.example.demo.controller;',
            '',
            'public class Helper {',
            '    public void keep() {',
            '        // keep method',
            '    }',
        ]
        # add many filler lines so the other method is far away
        for i in range(0, 60):
            old_content_lines.append('    // filler %d' % i)
        old_content_lines += [
            '',
            '    public void removeMe() {',
            '        // dangerous code',
            '    }',
            '}',
        ]
        old_content = '\n'.join(old_content_lines)
        file_io.write_file(extra_abs, old_content, overwrite=True)

        # patched content removes removeMe method
        new_lines = [
            'package com.example.demo.controller;',
            '',
            'public class Helper {',
            '    public void keep() {',
            '        // keep method',
            '    }',
            '',
            '}',
        ]
        new_content = '\n'.join(new_lines)

        patch_text = json.dumps({
            'files': [
                {
                    'path': extra_rel.replace('\\', '/'),
                    'patched_content': new_content,
                }
            ]
        })

        # analyze near the 'keep' method (line 4) but deletion occurs at lines ~8-9
        result = self.agent.validate_patch(self.repo_path, patch_text, source_info={'repo_relative_path': extra_rel.replace('\\', '/'), 'line_no': 4})

        self.assertFalse(result['valid'])
        # New validation should catch method deletion or massive line deletions
        self.assertTrue(any('deleted' in e.lower() or 'Too many lines' in e for e in result['errors']))

    def test_validate_patch_rejects_import_deletion(self):
        """Test that patches deleting imports are rejected."""
        # Create a Java file with multiple imports
        test_rel = os.path.join('src', 'main', 'java', 'com', 'example', 'OrderService.java')
        test_abs = os.path.join(self.repo_path, test_rel)
        os.makedirs(os.path.dirname(test_abs), exist_ok=True)
        
        old_content = '''package com.example;

import com.fixflow.mall.api.dto.CreateOrderRequest;
import com.fixflow.mall.domain.MallOrder;
import com.fixflow.mall.repo.OrderRepository;
import java.math.BigDecimal;
import org.springframework.stereotype.Service;

@Service
public class OrderService {
    private final OrderRepository orderRepository;

    public OrderService(OrderRepository orderRepository) {
        this.orderRepository = orderRepository;
    }

    public MallOrder createOrder(CreateOrderRequest req) {
        if (req.getAmount() == null) {
            throw new IllegalArgumentException("Amount required");
        }
        return orderRepository.save(new MallOrder());
    }

    public Long firstItemId(Long orderId) {
        MallOrder order = orderRepository.findById(orderId).orElseThrow();
        return order.getItemIds().get(0);
    }
}'''
        
        # Patch that removes imports and methods
        patched_content = '''package com.example;

import org.springframework.stereotype.Service;

@Service
public class OrderService {
    private OrderRepository orderRepository;

    public Long firstItemId(Long orderId) {
        MallOrder order = orderRepository.findById(orderId).orElseThrow();
        if (order.getItemIds() == null || order.getItemIds().isEmpty()) {
            return null;
        }
        return order.getItemIds().get(0);
    }
}'''
        
        file_io.write_file(test_abs, old_content, overwrite=True)
        
        patch_text = json.dumps({
            'files': [{
                'path': test_rel.replace('\\', '/'),
                'patched_content': patched_content,
            }]
        })
        
        result = self.agent.validate_patch(
            self.repo_path,
            patch_text,
            source_info={
                'repo_relative_path': test_rel.replace('\\', '/'),
                'line_no': 26,  # in firstItemId method
            }
        )
        
        self.assertFalse(result['valid'], "Patch that deletes imports and methods should be rejected")
        error_msgs = ' '.join(result['errors'])
        self.assertTrue(
            'Imports were deleted' in error_msgs or 'Too many lines' in error_msgs or 'deleted' in error_msgs.lower(),
            f"Expected error about deletions, got: {error_msgs}"
        )

    def test_validate_patch_rejects_line_number_prefixes(self):
        """Test that patches with line-number prefixes (LLM artifact) are rejected."""
        test_rel = os.path.join('src', 'main', 'java', 'com', 'example', 'LineNumberTest.java')
        test_abs = os.path.join(self.repo_path, test_rel)
        os.makedirs(os.path.dirname(test_abs), exist_ok=True)
        
        old_content = '''package com.example;

public class LineNumberTest {
    public void test() {
        System.out.println("hello");
    }
}'''
        
        # Patch that includes line-number prefixes (like what LLM does when it copies snippet)
        patched_with_line_numbers = '''59: package com.example;
60:
61: public class LineNumberTest {
62:     public void test() {
63:         System.out.println("fixed");
64:     }
65: }'''
        
        file_io.write_file(test_abs, old_content, overwrite=True)
        
        patch_text = json.dumps({
            'files': [{
                'path': test_rel.replace('\\', '/'),
                'patched_content': patched_with_line_numbers,
            }]
        })
        
        result = self.agent.validate_patch(
            self.repo_path,
            patch_text,
            source_info={
                'repo_relative_path': test_rel.replace('\\', '/'),
                'line_no': 4,
            }
        )
        
        self.assertFalse(result['valid'], "Patch with line-number prefixes should be rejected")
        error_msgs = ' '.join(result['errors'])
        self.assertIn('line-number prefix', error_msgs.lower(), f"Expected line-number error, got: {error_msgs}")


if __name__ == '__main__':
    unittest.main()



