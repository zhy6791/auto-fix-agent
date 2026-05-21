"""Unit tests for agents/tool_registry.py."""

import os
import shutil
import subprocess
import tempfile
import unittest

from agents.tool_registry import ToolDef, ToolRegistry
from tools import file_io, exec_cmd, git_manager


class MockLLMClient(object):
    def __init__(self, patch_text=''):
        self.patch_text = patch_text

    def generate_patch(self, prompt, max_tokens=1024):
        return self.patch_text

    def chat(self, messages, tools=None, max_tokens=4096, temperature=None):
        return {'content': '', 'tool_calls': None, 'finish_reason': 'stop'}


class TestToolRegistry(unittest.TestCase):

    def setUp(self):
        self.workdir = tempfile.mkdtemp()
        self.repo_path = os.path.join(self.workdir, 'repo')
        os.makedirs(self.repo_path)
        subprocess.check_call(['git', 'init'], cwd=self.repo_path)
        subprocess.check_call(['git', 'config', 'user.name', 'Test'], cwd=self.repo_path)
        subprocess.check_call(['git', 'config', 'user.email', 'test@test.com'], cwd=self.repo_path)

        source_dir = os.path.join(self.repo_path, 'src', 'main', 'java', 'com', 'example')
        os.makedirs(source_dir)
        self.source_file = os.path.join(source_dir, 'Foo.java')
        file_io.write_file(self.source_file,
            'package com.example;\n\npublic class Foo {\n    private int x = 0;\n    private int y = 1;\n\n    public void bar() {\n        System.out.println("bar");\n    }\n\n    public void baz() {\n        System.out.println("baz");\n    }\n}\n',
            overwrite=True)

        subprocess.check_call(['git', 'add', '.'], cwd=self.repo_path)
        subprocess.check_call(['git', 'commit', '-m', 'init'], cwd=self.repo_path)

        self.config = {
            'repo_path': self.repo_path,
            'logs_path': os.path.join(self.workdir, 'app.log'),
            'max_patch_lines': 40,
            'max_patch_hunks': 3,
            'max_hunk_lines': 24,
            'max_hunk_span': 40,
            'max_file_change_ratio': 0.35,
            'max_tokens': 8192,
            'command_whitelist': ['mvn', 'mvnw', 'gradle', 'gradlew'],
        }
        self.tools_dict = {
            'file_io': file_io,
            'exec_cmd': exec_cmd,
            'git_manager': git_manager,
        }
        self.llm_client = MockLLMClient()
        self.registry = ToolRegistry(self.config, self.tools_dict, self.llm_client)

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_all_tools_registered(self):
        expected = ['read_code', 'search_code', 'locate_from_stack', 'infer_source',
                    'edit_code', 'validate_patch', 'final_patch', 'abort']
        for name in expected:
            self.assertIsNotNone(self.registry.get(name), 'Tool %s not registered' % name)

    def test_list_tools(self):
        tools = self.registry.list_tools()
        self.assertEqual(len(tools), 8)
        names = [t.name for t in tools]
        self.assertIn('read_code', names)
        self.assertIn('final_patch', names)

    def test_get_openai_tool_schemas(self):
        schemas = self.registry.get_openai_tool_schemas()
        self.assertEqual(len(schemas), 8)
        for schema in schemas:
            self.assertEqual(schema['type'], 'function')
            self.assertIn('function', schema)
            self.assertIn('name', schema['function'])
            self.assertIn('description', schema['function'])
            self.assertIn('parameters', schema['function'])

    def test_get_text_tool_descriptions(self):
        text = self.registry.get_text_tool_descriptions()
        self.assertIn('read_code', text)
        self.assertIn('locate_from_stack', text)
        self.assertIn('final_patch', text)

    def test_execute_read_code(self):
        result = self.registry.execute('read_code', {'path': 'src/main/java/com/example/Foo.java'})
        self.assertIn('content', result)
        self.assertIn('package com.example', result['content'])

    def test_execute_read_code_not_found(self):
        result = self.registry.execute('read_code', {'path': 'Nonexistent.java'})
        self.assertIn('error', result)

    def test_execute_search_code(self):
        result = self.registry.execute('search_code', {'class_name': 'com.example.Foo'})
        self.assertTrue(result.get('found'))
        self.assertIn('Foo.java', result.get('path', ''))

    def test_execute_search_code_not_found(self):
        result = self.registry.execute('search_code', {'class_name': 'com.example.Missing'})
        self.assertFalse(result.get('found'))

    def test_execute_locate_from_stack(self):
        result = self.registry.execute('locate_from_stack', {
            'class_name': 'com.example.Foo',
            'method': 'bar',
            'line_no': 3,
        })
        self.assertIn('source_path', result)
        self.assertIn('full_source', result)
        self.assertIn('context_snippet', result)

    def test_execute_validate_patch_valid(self):
        patch = '--- a/src/main/java/com/example/Foo.java\n+++ b/src/main/java/com/example/Foo.java\n@@ -7,3 +7,3 @@\n     public void bar() {\n-        System.out.println("bar");\n+        System.out.println("fixed");\n     }\n'
        result = self.registry.execute('validate_patch', {
            'patch_text': patch,
            'source_info': {'repo_relative_path': 'src/main/java/com/example/Foo.java', 'line_no': 8},
        })
        self.assertTrue(result.get('valid'), 'Errors: %s' % result.get('errors'))

    def test_execute_validate_patch_invalid(self):
        result = self.registry.execute('validate_patch', {
            'patch_text': 'not a valid patch',
        })
        self.assertFalse(result.get('valid'))

    def test_execute_final_patch(self):
        result = self.registry.execute('final_patch', {
            'patch_text': '--- a/Foo.java\n+++ b/Foo.java\n',
            'source_info': {'class_name': 'Foo'},
        })
        self.assertEqual(result.get('signal'), 'final_patch')
        self.assertIn('patch_text', result)

    def test_execute_abort(self):
        result = self.registry.execute('abort', {'reason': 'Cannot fix'})
        self.assertEqual(result.get('signal'), 'abort')
        self.assertEqual(result.get('reason'), 'Cannot fix')

    def test_execute_unknown_tool(self):
        result = self.registry.execute('nonexistent_tool', {})
        self.assertIn('error', result)

    def test_edit_code_calls_llm(self):
        self.llm_client.patch_text = '--- a/test\n+++ b/test\n@@ -1 +1 @@\n-old\n+new\n'
        result = self.registry.execute('edit_code', {
            'raw_stack': 'NullPointerException at Foo.bar:3',
            'source_info': {'class_name': 'Foo', 'method': 'bar', 'line_no': 3, 'full_source': '...', 'context_snippet': '...', 'repo_relative_path': 'src/main/java/com/example/Foo.java'},
        })
        self.assertIn('patch_text', result)
        self.assertIn('prompt', result)


if __name__ == '__main__':
    unittest.main()
