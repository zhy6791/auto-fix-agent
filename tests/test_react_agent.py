"""Unit tests for agents/react_agent.py."""

import os
import shutil
import subprocess
import tempfile
import unittest

from agents.react_agent import ReActAgent
from agents.tool_registry import ToolRegistry
from tools import file_io, exec_cmd, git_manager


class MockLLMClient(object):
    """Mock LLM that returns pre-programmed chat responses."""

    def __init__(self, responses=None):
        self.responses = responses or []
        self.call_count = 0

    def generate_patch(self, prompt, max_tokens=1024):
        return 'NO_SAFE_PATCH: mock'

    def chat(self, messages, tools=None, max_tokens=4096, temperature=None):
        if self.call_count < len(self.responses):
            resp = self.responses[self.call_count]
            self.call_count += 1
            return resp
        # Default: abort to avoid infinite loop
        self.call_count += 1
        return {
            'content': 'Thought: Cannot proceed.\nAction: abort({"reason": "mock exhausted"})',
            'tool_calls': [{'name': 'abort', 'arguments': {'reason': 'mock exhausted'}}],
            'finish_reason': 'tool_calls',
        }


class TestReActAgent(unittest.TestCase):

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
            'package com.example;\npublic class Foo {\n    public String bar(String s) {\n        return s.length();\n    }\n}\n',
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
            'max_agent_iterations': 10,
            'command_whitelist': ['mvn'],
        }
        self.tools_dict = {
            'file_io': file_io,
            'exec_cmd': exec_cmd,
            'git_manager': git_manager,
        }

        self.context = {
            'raw_stack': 'java.lang.NullPointerException: Cannot invoke "String.length()" because "s" is null\n\tat com.example.Foo.bar(Foo.java:4)',
            'parsed_stack': [
                {'exception_type': 'java.lang.NullPointerException', 'class_name': 'com.example.Foo', 'method': 'bar', 'line_no': 4},
            ],
            'repo_path': self.repo_path,
            'dry_run': True,
        }

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_final_patch_exits_loop(self):
        """Agent should exit when final_patch is called."""
        responses = [
            {
                'content': 'Thought: Found the bug.\nAction: final_patch({})',
                'tool_calls': [{'name': 'final_patch', 'arguments': {
                    'patch_text': '--- a/src/main/java/com/example/Foo.java\n+++ b/src/main/java/com/example/Foo.java\n',
                    'source_info': {'class_name': 'Foo', 'method': 'bar', 'line_no': 4},
                }}],
                'finish_reason': 'tool_calls',
            },
        ]
        llm = MockLLMClient(responses)
        registry = ToolRegistry(self.config, self.tools_dict, llm)
        agent = ReActAgent(self.config, registry, llm, max_iterations=10)

        result = agent.run(self.context)

        self.assertFalse(result['aborted'])
        self.assertIsNotNone(result['final_patch'])
        self.assertEqual(result['iterations'], 1)

    def test_abort_exits_loop(self):
        """Agent should exit when abort is called."""
        responses = [
            {
                'content': 'Thought: Cannot fix.\nAction: abort({})',
                'tool_calls': [{'name': 'abort', 'arguments': {'reason': 'No safe fix'}}],
                'finish_reason': 'tool_calls',
            },
        ]
        llm = MockLLMClient(responses)
        registry = ToolRegistry(self.config, self.tools_dict, llm)
        agent = ReActAgent(self.config, registry, llm, max_iterations=10)

        result = agent.run(self.context)

        self.assertTrue(result['aborted'])
        self.assertEqual(result['abort_reason'], 'No safe fix')
        self.assertEqual(result['iterations'], 1)

    def test_max_iterations_reached(self):
        """Agent should abort when max iterations is reached."""
        # Return locate_from_stack repeatedly without calling final_patch
        responses = []
        for _ in range(5):
            responses.append({
                'content': 'Thought: Looking at code.\nAction: locate_from_stack({})',
                'tool_calls': [{'name': 'locate_from_stack', 'arguments': {
                    'class_name': 'com.example.Foo', 'method': 'bar', 'line_no': 4,
                }}],
                'finish_reason': 'tool_calls',
            })
        llm = MockLLMClient(responses)
        registry = ToolRegistry(self.config, self.tools_dict, llm)
        agent = ReActAgent(self.config, registry, llm, max_iterations=3)

        result = agent.run(self.context)

        self.assertTrue(result['aborted'])
        self.assertIn('maximum iterations', result['abort_reason'])
        self.assertEqual(result['iterations'], 3)

    def test_tool_execution_returns_observation(self):
        """Agent should receive tool execution results as observations."""
        responses = [
            {
                'content': 'Thought: Reading source.\nAction: read_code({})',
                'tool_calls': [{'name': 'read_code', 'arguments': {
                    'path': 'src/main/java/com/example/Foo.java',
                }}],
                'finish_reason': 'tool_calls',
            },
            {
                'content': 'Thought: Done.\nAction: abort({})',
                'tool_calls': [{'name': 'abort', 'arguments': {'reason': 'test done'}}],
                'finish_reason': 'tool_calls',
            },
        ]
        llm = MockLLMClient(responses)
        registry = ToolRegistry(self.config, self.tools_dict, llm)
        agent = ReActAgent(self.config, registry, llm, max_iterations=10)

        result = agent.run(self.context)

        self.assertEqual(result['iterations'], 2)
        self.assertEqual(len(result['tool_calls']), 2)
        # First tool call should have observation with file content
        first_call = result['tool_calls'][0]
        self.assertEqual(first_call['tool'], 'read_code')
        self.assertIn('Foo.java', first_call.get('result', ''))

    def test_thoughts_are_recorded(self):
        """Agent should record thoughts from each iteration."""
        responses = [
            {
                'content': 'Thought: First thought.\nAction: read_code({})',
                'tool_calls': [{'name': 'read_code', 'arguments': {'path': 'src/main/java/com/example/Foo.java'}}],
                'finish_reason': 'tool_calls',
            },
            {
                'content': 'Thought: Second thought.\nAction: abort({})',
                'tool_calls': [{'name': 'abort', 'arguments': {'reason': 'done'}}],
                'finish_reason': 'tool_calls',
            },
        ]
        llm = MockLLMClient(responses)
        registry = ToolRegistry(self.config, self.tools_dict, llm)
        agent = ReActAgent(self.config, registry, llm, max_iterations=10)

        result = agent.run(self.context)

        self.assertEqual(len(result['thoughts']), 2)
        self.assertEqual(result['thoughts'][0], 'First thought.')
        self.assertEqual(result['thoughts'][1], 'Second thought.')

    def test_scratchpad_grows(self):
        """Scratchpad should grow with each iteration."""
        responses = [
            {
                'content': 'Thought: Step 1.\nAction: read_code({})',
                'tool_calls': [{'name': 'read_code', 'arguments': {'path': 'src/main/java/com/example/Foo.java'}}],
                'finish_reason': 'tool_calls',
            },
            {
                'content': 'Thought: Step 2.\nAction: read_code({})',
                'tool_calls': [{'name': 'read_code', 'arguments': {'path': 'src/main/java/com/example/Foo.java'}}],
                'finish_reason': 'tool_calls',
            },
            {
                'content': 'Thought: Done.\nAction: abort({})',
                'tool_calls': [{'name': 'abort', 'arguments': {'reason': 'done'}}],
                'finish_reason': 'tool_calls',
            },
        ]
        llm = MockLLMClient(responses)
        registry = ToolRegistry(self.config, self.tools_dict, llm)
        agent = ReActAgent(self.config, registry, llm, max_iterations=10)

        agent.run(self.context)

        # system + initial user + 2*(assistant + observation) = 6
        # (abort is a signal tool, doesn't append to scratchpad)
        self.assertEqual(len(agent.scratchpad), 6)

    def test_context_window_management(self):
        """Scratchpad should be compressed when too large."""
        # Generate many iterations to trigger compression
        responses = []
        for i in range(15):
            responses.append({
                'content': 'Thought: Step %d.\nAction: read_code({})' % i,
                'tool_calls': [{'name': 'read_code', 'arguments': {'path': 'src/main/java/com/example/Foo.java'}}],
                'finish_reason': 'tool_calls',
            })
        llm = MockLLMClient(responses)
        registry = ToolRegistry(self.config, self.tools_dict, llm)
        agent = ReActAgent(self.config, registry, llm, max_iterations=15)

        agent.run(self.context)

        # After 15 iterations, scratchpad should be compressed
        # Max: 2 (system + initial) + 1 (summary) + 20 (last 10 exchanges) = 23
        self.assertLessEqual(len(agent.scratchpad), 23)

    def test_llm_call_failure_aborts(self):
        """Agent should abort if LLM call fails."""
        llm = MockLLMClient(responses=[])  # Will return abort by default
        # Override chat to simulate failure
        def failing_chat(messages, tools=None, max_tokens=4096, temperature=None):
            return None
        llm.chat = failing_chat

        registry = ToolRegistry(self.config, self.tools_dict, llm)
        agent = ReActAgent(self.config, registry, llm, max_iterations=10)

        result = agent.run(self.context)

        self.assertTrue(result['aborted'])
        self.assertIn('LLM call failed', result['abort_reason'])

    def test_full_flow_locate_edit_validate_finalize(self):
        """Test a complete agent flow: locate → edit → validate → final_patch."""
        mock_patch = '--- a/src/main/java/com/example/Foo.java\n+++ b/src/main/java/com/example/Foo.java\n@@ -3,2 +3,2 @@\n     public String bar(String s) {\n-        return s.length();\n+        return s == null ? "" : s.length();\n'

        class PatchMockLLM(object):
            def __init__(self):
                self.call_count = 0
                self.patch_text = mock_patch

            def generate_patch(self, prompt, max_tokens=1024):
                return self.patch_text

            def chat(self, messages, tools=None, max_tokens=4096, temperature=None):
                self.call_count += 1
                if self.call_count == 1:
                    return {
                        'content': 'Thought: Locating.\nAction: locate_from_stack({})',
                        'tool_calls': [{'name': 'locate_from_stack', 'arguments': {
                            'class_name': 'com.example.Foo', 'method': 'bar', 'line_no': 4,
                        }}],
                        'finish_reason': 'tool_calls',
                    }
                elif self.call_count == 2:
                    return {
                        'content': 'Thought: Editing.\nAction: edit_code({})',
                        'tool_calls': [{'name': 'edit_code', 'arguments': {
                            'raw_stack': 'NPE', 'source_info': {'class_name': 'Foo'},
                        }}],
                        'finish_reason': 'tool_calls',
                    }
                elif self.call_count == 3:
                    return {
                        'content': 'Thought: Validating.\nAction: validate_patch({})',
                        'tool_calls': [{'name': 'validate_patch', 'arguments': {
                            'patch_text': mock_patch,
                            'source_info': {'repo_relative_path': 'src/main/java/com/example/Foo.java', 'line_no': 4},
                        }}],
                        'finish_reason': 'tool_calls',
                    }
                else:
                    return {
                        'content': 'Thought: Done.\nAction: final_patch({})',
                        'tool_calls': [{'name': 'final_patch', 'arguments': {
                            'patch_text': mock_patch,
                            'source_info': {'class_name': 'Foo', 'method': 'bar', 'line_no': 4, 'repo_relative_path': 'src/main/java/com/example/Foo.java'},
                        }}],
                        'finish_reason': 'tool_calls',
                    }

        llm = PatchMockLLM()
        registry = ToolRegistry(self.config, self.tools_dict, llm)
        agent = ReActAgent(self.config, registry, llm, max_iterations=10)

        result = agent.run(self.context)

        self.assertFalse(result['aborted'])
        self.assertIsNotNone(result['final_patch'])
        self.assertIn('Foo.java', result['source_info'].get('repo_relative_path', ''))
        self.assertEqual(result['iterations'], 4)
        self.assertEqual(len(result['tool_calls']), 4)

    def test_parse_response_function_calling(self):
        """Test parsing function calling response."""
        registry = ToolRegistry(self.config, self.tools_dict, MockLLMClient())
        agent = ReActAgent(self.config, registry, MockLLMClient(), max_iterations=10)

        response = {
            'content': 'Thought: I need to read the file.\nAction: read_code({})',
            'tool_calls': [{'name': 'read_code', 'arguments': {'path': 'Foo.java'}}],
            'finish_reason': 'tool_calls',
        }
        thought, name, args = agent._parse_response(response)
        self.assertEqual(thought, 'I need to read the file.')
        self.assertEqual(name, 'read_code')
        self.assertEqual(args, {'path': 'Foo.java'})

    def test_parse_response_text_mode(self):
        """Test parsing text-based response."""
        registry = ToolRegistry(self.config, self.tools_dict, MockLLMClient())
        agent = ReActAgent(self.config, registry, MockLLMClient(), max_iterations=10)

        response = {
            'content': 'Thought: Analyzing the stack trace.\nAction: locate_from_stack({"class_name": "com.example.Foo", "method": "bar", "line_no": 4})',
            'tool_calls': None,
            'finish_reason': 'stop',
        }
        thought, name, args = agent._parse_response(response)
        self.assertEqual(thought, 'Analyzing the stack trace.')
        self.assertEqual(name, 'locate_from_stack')
        self.assertEqual(args['class_name'], 'com.example.Foo')

    def test_parse_response_no_action(self):
        """Test parsing response with no action."""
        registry = ToolRegistry(self.config, self.tools_dict, MockLLMClient())
        agent = ReActAgent(self.config, registry, MockLLMClient(), max_iterations=10)

        response = {
            'content': 'I am thinking about this problem.',
            'tool_calls': None,
            'finish_reason': 'stop',
        }
        thought, name, args = agent._parse_response(response)
        self.assertIsNone(name)


if __name__ == '__main__':
    unittest.main()
