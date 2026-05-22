"""Tool registry for the ReAct agent loop.

Defines ToolDef for each callable tool and ToolRegistry for dispatch.
"""

import json
import logging
import os

from tools import file_io, git_manager
from agents.log_extraction import stacktrace_parser, source_locator, exception_inference
from agents.agent_loop import prompt_builder
from agents.post_processing import patch_validator, test_generator

logger = logging.getLogger(__name__)


class ToolDef:
    """Definition of a single agent tool."""

    def __init__(self, name, description, parameters, fn, category):
        self.name = name
        self.description = description
        self.parameters = parameters  # JSON Schema dict
        self.fn = fn                  # callable
        self.category = category      # "read" | "analyze" | "generate" | "signal"


class ToolRegistry:
    """Registry of tools available to the ReAct agent."""

    def __init__(self, config, tools_dict, llm_client):
        self.config = config
        self.tools_dict = tools_dict
        self.llm_client = llm_client
        self.repo_path = config.get('repo_path', '')
        self._tools = {}
        self._register_all()

    def _register_all(self):
        repo_path = self.repo_path
        config = self.config
        llm_client = self.llm_client
        file_io_mod = self.tools_dict.get('file_io', file_io)

        # 1. read_code
        self.register(ToolDef(
            name='read_code',
            description='Read the contents of a file in the repository. Returns the full text.',
            parameters={
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': 'Repo-relative or absolute file path'},
                },
                'required': ['path'],
            },
            fn=lambda **kw: _read_code(repo_path, file_io_mod, kw.get('path', '')),
            category='read',
        ))

        # 2. search_code
        self.register(ToolDef(
            name='search_code',
            description='Locate a Java source file in the repository by class name or file path. Returns the absolute path or null.',
            parameters={
                'type': 'object',
                'properties': {
                    'class_name': {'type': 'string', 'description': 'Fully qualified Java class name (e.g. com.example.Foo)'},
                    'file_path': {'type': 'string', 'description': 'Relative file path (e.g. src/main/java/com/example/Foo.java)'},
                },
            },
            fn=lambda **kw: _search_code(repo_path, kw.get('class_name'), kw.get('file_path')),
            category='read',
        ))

        # 3. locate_from_stack
        self.register(ToolDef(
            name='locate_from_stack',
            description='Find the source file for a given Java stack frame. Returns file path, class name, method, line number, context snippet (7 lines around problem), and full source code.',
            parameters={
                'type': 'object',
                'properties': {
                    'class_name': {'type': 'string', 'description': 'Fully qualified Java class name from the stack frame'},
                    'method': {'type': 'string', 'description': 'Method name from the stack frame'},
                    'line_no': {'type': 'integer', 'description': 'Line number from the stack frame'},
                },
                'required': ['class_name'],
            },
            fn=lambda **kw: _locate_from_stack(repo_path, file_io_mod, kw),
            category='analyze',
        ))

        # 4. infer_source
        self.register(ToolDef(
            name='infer_source',
            description='When the stack trace contains only framework code (Spring/Tomcat), use LLM to infer which application class/method is responsible based on the exception message. Returns source_info or null.',
            parameters={
                'type': 'object',
                'properties': {
                    'raw_stack': {'type': 'string', 'description': 'The raw exception stack trace text'},
                    'parsed_stack': {'type': 'array', 'description': 'Parsed stack frames (list of dicts)'},
                },
                'required': ['raw_stack', 'parsed_stack'],
            },
            fn=lambda **kw: _infer_source(repo_path, config, llm_client, file_io_mod, kw),
            category='analyze',
        ))

        # 5. edit_code
        self.register(ToolDef(
            name='edit_code',
            description='Generate a minimal unified-diff patch to fix the identified bug. Internally calls LLM with the exception info and source code. Returns patch_text. Does NOT modify files.',
            parameters={
                'type': 'object',
                'properties': {
                    'raw_stack': {'type': 'string', 'description': 'The raw exception stack trace text'},
                    'source_info': {
                        'type': 'object',
                        'description': 'Source info dict from locate_from_stack or infer_source (contains class_name, method, line_no, context_snippet, full_source, repo_relative_path)',
                    },
                },
                'required': ['raw_stack', 'source_info'],
            },
            fn=lambda **kw: _edit_code(config, llm_client, kw),
            category='generate',
        ))

        # 6. validate_patch
        self.register(ToolDef(
            name='validate_patch',
            description='Validate a patch before applying. Checks: unified diff format, path boundaries, hunk sizes, Java structure preservation, import/package safety. Returns {valid: bool, errors: [...]}.',
            parameters={
                'type': 'object',
                'properties': {
                    'patch_text': {'type': 'string', 'description': 'The unified diff patch text to validate'},
                    'source_info': {
                        'type': 'object',
                        'description': 'Optional source_info dict for additional structural checks',
                    },
                },
                'required': ['patch_text'],
            },
            fn=lambda **kw: _validate_patch(config, repo_path, file_io_mod, kw),
            category='analyze',
        ))

        # 7. generate_test
        self.register(ToolDef(
            name='generate_test',
            description='Generate JUnit 5 unit test for the fixed class. Use this AFTER validate_patch passes and BEFORE final_patch. Returns test_code that can be reviewed before final_patch.',
            parameters={
                'type': 'object',
                'properties': {
                    'source_info': {
                        'type': 'object',
                        'description': 'Source info dict (contains class_name, method, line_no, repo_relative_path, full_source)',
                    },
                    'patch_text': {'type': 'string', 'description': 'The validated unified diff patch'},
                    'raw_stack': {'type': 'string', 'description': 'The raw exception stack trace'},
                },
                'required': ['source_info', 'patch_text', 'raw_stack'],
            },
            fn=lambda **kw: _generate_test(config, repo_path, llm_client, file_io_mod, kw),
            category='generate',
        ))

        # 8. final_patch
        self.register(ToolDef(
            name='final_patch',
            description='Signal that the agent has completed the fix. Provide the final validated patch, source info, and optionally test_code. This exits the agent loop.',
            parameters={
                'type': 'object',
                'properties': {
                    'patch_text': {'type': 'string', 'description': 'The final unified diff patch'},
                    'source_info': {
                        'type': 'object',
                        'description': 'Source info dict (contains class_name, method, line_no, repo_relative_path, etc.)',
                    },
                    'test_code': {'type': 'string', 'description': 'Optional: generated JUnit test code from generate_test tool'},
                },
                'required': ['patch_text', 'source_info'],
            },
            fn=lambda **kw: {'signal': 'final_patch', 'patch_text': kw.get('patch_text', ''), 'source_info': kw.get('source_info', {}), 'test_code': kw.get('test_code', '')},
            category='signal',
        ))

        # 9. abort
        self.register(ToolDef(
            name='abort',
            description='Signal that the agent cannot safely fix the issue. This exits the agent loop with a failure reason.',
            parameters={
                'type': 'object',
                'properties': {
                    'reason': {'type': 'string', 'description': 'Why the fix cannot be completed'},
                },
                'required': ['reason'],
            },
            fn=lambda **kw: {'signal': 'abort', 'reason': kw.get('reason', 'Unknown')},
            category='signal',
        ))

    def register(self, tool_def):
        self._tools[tool_def.name] = tool_def

    def get(self, name):
        return self._tools.get(name)

    def list_tools(self):
        return list(self._tools.values())

    def get_openai_tool_schemas(self):
        """Export tools as OpenAI function calling schemas."""
        schemas = []
        for tool in self._tools.values():
            schemas.append({
                'type': 'function',
                'function': {
                    'name': tool.name,
                    'description': tool.description,
                    'parameters': tool.parameters,
                },
            })
        return schemas

    def get_text_tool_descriptions(self):
        """Export tools as human-readable text for text-based tool calling."""
        lines = []
        for tool in self._tools.values():
            params = tool.parameters.get('properties', {})
            required = tool.parameters.get('required', [])
            param_parts = []
            for pname, pdef in params.items():
                req = '*' if pname in required else ''
                param_parts.append('%s%s: %s (%s)' % (pname, req, pdef.get('description', ''), pdef.get('type', 'string')))
            lines.append('- %s: %s\n  Parameters: %s' % (tool.name, tool.description, '; '.join(param_parts) if param_parts else 'none'))
        return '\n'.join(lines)

    def execute(self, name, args):
        """Execute a tool by name with given arguments. Returns the result."""
        tool = self._tools.get(name)
        if not tool:
            return {'error': 'Unknown tool: %s' % name}
        try:
            result = tool.fn(**(args or {}))
            return result
        except Exception as e:
            logger.exception('Tool %s execution failed', name)
            return {'error': '%s: %s' % (type(e).__name__, str(e))}


# ── Tool implementation helpers ──────────────────────────────────

def _read_code(repo_path, file_io_mod, path):
    if not path:
        return {'error': 'path is required'}
    if not os.path.isabs(path):
        path = os.path.join(repo_path, path)
    try:
        content = file_io_mod.read_file(path)
        return {'content': content, 'path': path}
    except FileNotFoundError:
        return {'error': 'File not found: %s' % path}


def _search_code(repo_path, class_name, file_path):
    result = source_locator.locate_file_by_class_or_path(repo_path, class_name, file_path)
    if result:
        return {'found': True, 'path': result}
    return {'found': False, 'error': 'File not found for class=%s path=%s' % (class_name, file_path)}


def _locate_from_stack(repo_path, file_io_mod, kw):
    frame = {
        'class_name': kw.get('class_name', ''),
        'method': kw.get('method'),
        'line_no': kw.get('line_no'),
    }
    try:
        info = source_locator.find_source_location(repo_path, frame, file_io_mod)
        return info
    except FileNotFoundError as e:
        return {'error': str(e)}


def _infer_source(repo_path, config, llm_client, file_io_mod, kw):
    raw_stack = kw.get('raw_stack', '')
    parsed_stack = kw.get('parsed_stack', [])
    result = exception_inference.infer_from_exception_message(
        repo_path, raw_stack, parsed_stack, config, llm_client, file_io_mod
    )
    if result:
        return result
    return {'error': 'Could not infer source from exception message'}


def _edit_code(config, llm_client, kw):
    raw_stack = kw.get('raw_stack', '')
    source_info = kw.get('source_info', {})
    prompt = prompt_builder.build_prompt(config, raw_stack, source_info)
    max_tokens = int(config.get('max_tokens', 8192))
    patch_text = llm_client.generate_patch(prompt, max_tokens=max_tokens)
    return {'patch_text': patch_text, 'prompt': prompt}


def _validate_patch(config, repo_path, file_io_mod, kw):
    patch_text = kw.get('patch_text', '')
    source_info = kw.get('source_info')
    result = patch_validator.validate_patch(config, repo_path, patch_text, source_info, file_io_mod)
    return result


def _generate_test(config, repo_path, llm_client, file_io_mod, kw):
    """Generate JUnit test code for the fixed class."""
    source_info = kw.get('source_info', {})
    patch_text = kw.get('patch_text', '')
    raw_stack = kw.get('raw_stack', '')

    if not source_info or not patch_text:
        return {'error': 'source_info and patch_text are required', 'test_code': ''}

    try:
        # Collect project context
        tools = {'file_io': file_io_mod}
        context = test_generator.collect_project_context(repo_path, source_info, tools)

        # Generate test code
        test_code = test_generator.generate_test(
            llm_client, config, source_info, patch_text, raw_stack,
            project_context=context
        )

        if test_code:
            return {'test_code': test_code, 'success': True}
        else:
            return {'test_code': '', 'success': False, 'error': 'LLM failed to generate valid test code'}
    except Exception as e:
        logger.exception('Test generation failed in agent tool')
        return {'test_code': '', 'success': False, 'error': str(e)}
