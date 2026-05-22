"""agents.post_processing.test_generator 单元测试。"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from agents.post_processing.test_generator import (
    build_test_generation_prompt,
    generate_test,
    write_test_file,
    run_test_generation,
)


class TestBuildTestGenerationPrompt(unittest.TestCase):
    """build_test_generation_prompt 测试。"""

    def setUp(self):
        self.source_info = {
            'class_name': 'com.example.service.UserService',
            'method': 'getUser',
            'line_no': 27,
            'repo_relative_path': 'src/main/java/com/example/service/UserService.java',
            'full_source': 'package com.example.service;\n\npublic class UserService {\n    public User getUser(Long id) {\n        return repository.findById(id).orElse(null);\n    }\n}',
        }
        self.patch_text = '--- a/src/main/java/com/example/service/UserService.java\n+++ b/src/main/java/com/example/service/UserService.java\n@@ -4,1 +4,2 @@\n-        return repository.findById(id).orElse(null);\n+        return repository.findById(id).orElseThrow(() -> new NotFoundException("User not found: " + id));'
        self.raw_stack = 'java.lang.NullPointerException: null\n\tat com.example.service.UserService.getUser(UserService.java:27)'

    def test_basic_prompt_contains_required_info(self):
        """prompt 包含类名、方法、异常信息。"""
        prompt = build_test_generation_prompt(
            self.source_info, self.patch_text, self.raw_stack
        )
        self.assertIn('UserService', prompt)
        self.assertIn('getUser', prompt)
        self.assertIn('NullPointerException', prompt)
        self.assertIn('JUnit 5', prompt)
        self.assertIn('@Test', prompt)

    def test_prompt_contains_class_name(self):
        """prompt 包含完整类名。"""
        prompt = build_test_generation_prompt(
            self.source_info, self.patch_text, self.raw_stack
        )
        self.assertIn('com.example.service.UserService', prompt)

    def test_prompt_contains_source_path(self):
        """prompt 包含源文件路径。"""
        prompt = build_test_generation_prompt(
            self.source_info, self.patch_text, self.raw_stack
        )
        self.assertIn('src/main/java/com/example/service/UserService.java', prompt)

    def test_prompt_contains_full_source(self):
        """prompt 包含完整源码。"""
        prompt = build_test_generation_prompt(
            self.source_info, self.patch_text, self.raw_stack
        )
        self.assertIn('public class UserService', prompt)

    def test_prompt_with_existing_test_content(self):
        """有已有测试时 prompt 包含已有内容。"""
        existing = 'package com.example.service;\n\nimport org.junit.jupiter.api.Test;\n\nclass UserServiceTest {\n    @Test\n    void existingTest() {}\n}'
        prompt = build_test_generation_prompt(
            self.source_info, self.patch_text, self.raw_stack,
            existing_test_content=existing
        )
        self.assertIn('当前测试文件', prompt)
        self.assertIn('existingTest', prompt)
        self.assertIn('保留所有已有测试方法', prompt)

    def test_prompt_without_existing_test_content(self):
        """无已有测试时省略该段。"""
        prompt = build_test_generation_prompt(
            self.source_info, self.patch_text, self.raw_stack,
            existing_test_content=None
        )
        self.assertNotIn('已存在的测试文件', prompt)

    def test_prompt_output_requirements(self):
        """prompt 包含输出格式要求。"""
        prompt = build_test_generation_prompt(
            self.source_info, self.patch_text, self.raw_stack
        )
        self.assertIn('UserServiceTest', prompt)
        self.assertIn('只返回完整的 Java 测试类源码', prompt)

    def test_prompt_truncates_long_stack(self):
        """长堆栈被截断到 1000 字符。"""
        long_stack = 'A' * 2000
        prompt = build_test_generation_prompt(
            self.source_info, self.patch_text, long_stack
        )
        # 1000 chars from the stack + surrounding text
        self.assertIn('A' * 1000, prompt)
        self.assertNotIn('A' * 1001, prompt)

    def test_prompt_truncates_long_source(self):
        """超过 200 行的源码被截断。"""
        long_source = '\n'.join(['line %d' % i for i in range(300)])
        source_info = dict(self.source_info, full_source=long_source)
        prompt = build_test_generation_prompt(
            source_info, self.patch_text, self.raw_stack
        )
        self.assertIn('省略中间部分', prompt)

    def test_prompt_with_constructor_info(self):
        """有构造函数信息时 prompt 包含该信息。"""
        project_context = {
            'constructor_info': '该类使用构造函数注入，参数为: UserRepository userRepository',
        }
        prompt = build_test_generation_prompt(
            self.source_info, self.patch_text, self.raw_stack,
            project_context=project_context
        )
        self.assertIn('构造函数注入信息', prompt)
        self.assertIn('该类使用构造函数注入', prompt)

    def test_prompt_without_constructor_info(self):
        """无构造函数信息时省略该段。"""
        prompt = build_test_generation_prompt(
            self.source_info, self.patch_text, self.raw_stack
        )
        self.assertNotIn('构造函数注入信息', prompt)


class TestGenerateTest(unittest.TestCase):
    """generate_test 测试。"""

    def setUp(self):
        self.source_info = {
            'class_name': 'com.example.UserService',
            'method': 'getUser',
            'repo_relative_path': 'src/main/java/com/example/UserService.java',
        }
        self.config = {'test_gen_max_tokens': 4096}

    def test_returns_code_on_success(self):
        """成功时返回 Java 代码。"""
        mock_llm = Mock()
        mock_llm.chat.return_value = {
            'content': 'package com.example;\nimport org.junit.jupiter.api.Test;\nclass UserServiceTest { @Test void testGetUser() {} }',
            'tool_calls': None,
            'finish_reason': 'stop',
        }
        result = generate_test(
            mock_llm, self.config, self.source_info, '...', '...'
        )
        self.assertIn('@Test', result)
        self.assertIn('class UserServiceTest', result)

    def test_strips_markdown_fences(self):
        """清理 markdown 围栏。"""
        mock_llm = Mock()
        mock_llm.chat.return_value = {
            'content': '```java\npackage com.example;\nimport org.junit.jupiter.api.Test;\nclass UserServiceTest { @Test void t() {} }\n```',
            'tool_calls': None,
            'finish_reason': 'stop',
        }
        result = generate_test(
            mock_llm, self.config, self.source_info, '...', '...'
        )
        self.assertNotIn('```', result)
        self.assertIn('@Test', result)

    def test_returns_empty_on_error_finish_reason(self):
        """finish_reason 为 error 时返回空串。"""
        mock_llm = Mock()
        mock_llm.chat.return_value = {
            'content': 'NO_SAFE_PATCH: error',
            'tool_calls': None,
            'finish_reason': 'error',
        }
        result = generate_test(
            mock_llm, self.config, self.source_info, '...', '...'
        )
        self.assertEqual(result, '')

    def test_returns_empty_on_empty_content(self):
        """空内容时返回空串。"""
        mock_llm = Mock()
        mock_llm.chat.return_value = {
            'content': '',
            'tool_calls': None,
            'finish_reason': 'stop',
        }
        result = generate_test(
            mock_llm, self.config, self.source_info, '...', '...'
        )
        self.assertEqual(result, '')

    def test_returns_empty_on_non_java_content(self):
        """非 Java 代码时返回空串。"""
        mock_llm = Mock()
        mock_llm.chat.return_value = {
            'content': 'Here is the test you requested.',
            'tool_calls': None,
            'finish_reason': 'stop',
        }
        result = generate_test(
            mock_llm, self.config, self.source_info, '...', '...'
        )
        self.assertEqual(result, '')

    def test_passes_existing_content_to_prompt(self):
        """将已有测试内容传入 prompt。"""
        mock_llm = Mock()
        mock_llm.chat.return_value = {
            'content': 'class FooTest { @Test void t() {} }',
            'tool_calls': None,
            'finish_reason': 'stop',
        }
        generate_test(
            mock_llm, self.config, self.source_info, '...', '...',
            existing_test_content='existing code'
        )
        # Verify the prompt in the chat call contains existing content
        call_args = mock_llm.chat.call_args
        messages = call_args[0][0]
        user_msg = messages[1]['content']
        self.assertIn('existing code', user_msg)

    def test_uses_config_max_tokens(self):
        """使用配置中的 max_tokens。"""
        mock_llm = Mock()
        mock_llm.chat.return_value = {
            'content': 'class FooTest { @Test void t() {} }',
            'tool_calls': None,
            'finish_reason': 'stop',
        }
        config = {'test_gen_max_tokens': 2048}
        generate_test(
            mock_llm, config, self.source_info, '...', '...'
        )
        call_args = mock_llm.chat.call_args
        self.assertEqual(call_args[1]['max_tokens'], 2048)


class TestWriteTestFile(unittest.TestCase):
    """write_test_file 测试。"""

    def setUp(self):
        self.workdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_creates_file_and_dirs(self):
        """创建文件和中间目录。"""
        test_rel = 'src/test/java/com/example/UserServiceTest.java'
        code = 'package com.example;\n\nclass UserServiceTest {}'
        result = write_test_file(self.workdir, test_rel, code)
        self.assertTrue(os.path.exists(result))
        with open(result, 'r', encoding='utf-8') as f:
            self.assertEqual(f.read(), code)

    def test_returns_absolute_path(self):
        """返回绝对路径。"""
        test_rel = 'src/test/java/FooTest.java'
        result = write_test_file(self.workdir, test_rel, 'class FooTest {}')
        self.assertTrue(os.path.isabs(result))

    def test_overwrites_existing_file(self):
        """覆盖已有文件。"""
        test_rel = 'src/test/java/FooTest.java'
        write_test_file(self.workdir, test_rel, 'old content')
        write_test_file(self.workdir, test_rel, 'new content')
        abs_path = os.path.join(self.workdir, test_rel)
        with open(abs_path, 'r', encoding='utf-8') as f:
            self.assertEqual(f.read(), 'new content')

    def test_returns_empty_on_failure(self):
        """失败时返回空字符串。"""
        # Invalid path (file as directory)
        result = write_test_file('/nonexistent/\x00path', 'test.java', 'code')
        self.assertEqual(result, '')


class TestRunTestGeneration(unittest.TestCase):
    """run_test_generation 完整流程测试。"""

    def setUp(self):
        self.workdir = tempfile.mkdtemp()
        self.config = {'test_gen_max_tokens': 4096}
        self.source_info = {
            'class_name': 'com.example.UserService',
            'method': 'getUser',
            'repo_relative_path': 'src/main/java/com/example/UserService.java',
        }
        self.mock_llm = Mock()
        self.mock_llm.chat.return_value = {
            'content': 'package com.example;\nimport org.junit.jupiter.api.Test;\nclass UserServiceTest { @Test void testGetUser() {} }',
            'tool_calls': None,
            'finish_reason': 'stop',
        }
        self.mock_tools = {
            'file_io': Mock(),
            'git_manager': Mock(),
        }
        self.mock_tools['git_manager'].commit_changes.return_value = True

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    @patch('agents.post_processing.test_generator._derive_test_path')
    def test_full_flow_success(self, mock_derive):
        """完整流程：推导路径、生成、写入、提交。"""
        mock_derive.return_value = 'src/test/java/com/example/UserServiceTest.java'
        result = run_test_generation(
            self.config, self.workdir, self.source_info,
            '...', '...', self.mock_llm, self.mock_tools,
        )
        self.assertTrue(result['generated'])
        self.assertTrue(result['committed'])
        self.assertEqual(result['test_path'], 'src/test/java/com/example/UserServiceTest.java')
        self.assertIsNone(result['error'])

    @patch('agents.post_processing.test_generator._derive_test_path')
    def test_reads_existing_test_file(self, mock_derive):
        """读取已有测试文件。"""
        test_path = os.path.join(self.workdir, 'src', 'test', 'java', 'com', 'example', 'UserServiceTest.java')
        os.makedirs(os.path.dirname(test_path), exist_ok=True)
        with open(test_path, 'w') as f:
            f.write('existing test content')

        mock_derive.return_value = 'src/test/java/com/example/UserServiceTest.java'
        self.mock_tools['file_io'].read_file.return_value = 'existing test content'
        run_test_generation(
            self.config, self.workdir, self.source_info,
            '...', '...', self.mock_llm, self.mock_tools,
        )

        # Verify existing content was passed to LLM
        call_args = self.mock_llm.chat.call_args
        messages = call_args[0][0]
        user_msg = messages[1]['content']
        self.assertIn('existing test content', user_msg)

    @patch('agents.post_processing.test_generator._derive_test_path')
    def test_continues_on_llm_failure(self, mock_derive):
        """LLM 失败时不阻断流程。"""
        mock_derive.return_value = 'src/test/java/com/example/UserServiceTest.java'
        self.mock_llm.chat.return_value = {
            'content': '',
            'tool_calls': None,
            'finish_reason': 'stop',
        }
        result = run_test_generation(
            self.config, self.workdir, self.source_info,
            '...', '...', self.mock_llm, self.mock_tools,
        )
        self.assertFalse(result['generated'])
        self.assertIsNotNone(result['error'])

    @patch('agents.post_processing.test_generator._derive_test_path')
    def test_continues_on_commit_failure(self, mock_derive):
        """提交失败时仍然标记为已生成。"""
        mock_derive.return_value = 'src/test/java/com/example/UserServiceTest.java'
        self.mock_tools['git_manager'].commit_changes.return_value = False
        result = run_test_generation(
            self.config, self.workdir, self.source_info,
            '...', '...', self.mock_llm, self.mock_tools,
        )
        self.assertTrue(result['generated'])
        self.assertFalse(result['committed'])


if __name__ == '__main__':
    unittest.main()
