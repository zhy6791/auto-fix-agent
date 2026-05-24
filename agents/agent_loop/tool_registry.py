"""Tool registry for the ReAct agent loop.

Defines ToolDef for each callable tool and ToolRegistry for dispatch.
"""

import json
import logging
import os

from tools import file_io, git_manager
from agents.log_extraction import stacktrace_parser, source_locator, exception_inference
from agents.log_extraction.search_manager import SearchManager
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
            description='Infer which application class is responsible for the exception. Internally uses code graph search first (high accuracy), falls back to LLM inference if graph search fails. Returns source_info or null.',
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
    if os.path.isdir(path):
        entries = []
        for name in sorted(os.listdir(path)):
            prefix = '[DIR] ' if os.path.isdir(os.path.join(path, name)) else '[FILE]'
            entries.append('%s %s' % (prefix, name))
        return {'content': '\n'.join(entries), 'path': path, 'is_directory': True}
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

    # 优先使用代码图谱搜索（比LLM推断更可靠）
    graph_result = _try_graph_search(repo_path, config, parsed_stack, file_io_mod, raw_stack=raw_stack)
    if graph_result:
        logger.info('infer_source: 代码图谱搜索成功定位到 %s', graph_result.get('class_name', ''))
        return graph_result

    # 回退到LLM推断
    logger.info('infer_source: 代码图谱搜索无结果，使用LLM推断')
    result = exception_inference.infer_from_exception_message(
        repo_path, raw_stack, parsed_stack, config, llm_client, file_io_mod
    )
    if result:
        return result
    return {'error': 'Could not infer source from exception message'}


def _try_graph_search(repo_path, config, parsed_stack, file_io_mod, raw_stack=''):
    """使用代码图谱搜索定位源码，成功返回source_info，失败返回None

    两阶段策略：
    1. 快速评分：关键词匹配，筛选出 top-N 候选
    2. 深度分析：对 top-N 候选做源码级结构分析，精准定位
    """
    try:
        orcaloca_config = config.get('orcaloca', {})
        if not orcaloca_config.get('enabled', False):
            return None

        from agents.code_graph.repo_graph import RepoGraph
        graph = RepoGraph()
        build_timeout = orcaloca_config.get('build_timeout', 60)
        max_files = orcaloca_config.get('max_files', 10000)
        graph.build(repo_path, max_files=max_files, timeout=build_timeout)

        if not graph.is_built():
            return None

        search_manager = SearchManager(graph, repo_path)

        # 从parsed_stack提取异常类型
        exception_type = ''
        if parsed_stack:
            exception_type = parsed_stack[0].get('exception_type', '')
            if '.' in exception_type:
                exception_type = exception_type.split('.')[-1]

        # 收集候选
        candidates = _collect_candidates(search_manager, exception_type)
        if not candidates:
            return None

        # 提取关键词
        keywords = _extract_keywords_from_exception(raw_stack or '', parsed_stack)

        # 阶段1: 快速关键词评分，筛选 top-N
        top_candidates = _quick_score_candidates(repo_path, candidates, keywords, top_n=5)
        if not top_candidates:
            return None

        # 阶段2: 深度源码分析，从 top-N 中精准定位
        best = _deep_analyze_candidates(repo_path, top_candidates, raw_stack, keywords)
        if best:
            return _search_result_to_source_info(best, repo_path, file_io_mod)

        # 回退：返回快速评分第一名
        return _search_result_to_source_info(top_candidates[0], repo_path, file_io_mod)
    except Exception as e:
        logger.debug('图搜索回退失败: %s', e)
        return None


import re


def _collect_candidates(search_manager, exception_type):
    """收集所有候选文件：注解搜索 + 异常类型搜索，去重"""
    seen = set()
    candidates = []

    for ann in ['@RestController', '@Controller', '@Service', '@Component', '@Repository']:
        for r in search_manager.search_by_annotation(ann):
            if r.class_name not in seen:
                seen.add(r.class_name)
                candidates.append(r)

    if exception_type:
        for r in search_manager.search_by_exception_type(exception_type):
            if r.class_name not in seen:
                seen.add(r.class_name)
                candidates.append(r)

    return candidates


def _extract_keywords_from_exception(raw_stack, parsed_stack):
    """从异常消息和堆栈中提取关键词

    提取：引号标识符、驼峰命名、应用层类名/方法名
    """
    keywords = set()

    for m in re.finditer(r"['\"](\w+)['\"]", raw_stack):
        keywords.add(m.group(1))

    for m in re.finditer(r'\b([a-z][a-zA-Z]{1,}[A-Z]\w*)\b', raw_stack):
        keywords.add(m.group(1))

    framework_prefixes = ('org.springframework', 'org.apache', 'java.', 'javax.',
                          'jakarta.', 'sun.', 'com.sun.', 'org.eclipse.')
    if parsed_stack:
        for frame in parsed_stack[:5]:
            cls = frame.get('class_name', '')
            if cls and not any(cls.startswith(p) for p in framework_prefixes):
                keywords.add(cls.split('.')[-1])
                method = frame.get('method', '')
                if method:
                    keywords.add(method)

    return keywords


def _quick_score_candidates(repo_path, candidates, keywords, top_n=5):
    """快速评分：关键词在文件名和内容中的出现次数，返回 top-N"""
    scored = []
    for candidate in candidates:
        score = 0
        file_path = candidate.file_path
        if not os.path.isabs(file_path):
            file_path = os.path.join(repo_path, file_path)

        basename = os.path.basename(file_path).replace('.java', '')
        for kw in keywords:
            if kw.lower() in basename.lower():
                score += 20

        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                source = f.read()
            source_lower = source.lower()
            for kw in keywords:
                if len(kw) >= 3 and kw.lower() in source_lower:
                    score += 10
        except OSError:
            pass

        scored.append((score, candidate))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored[:top_n] if scored[0][0] > 0]


# ── 深度分析器注册表 ──────────────────────────────────────────
# key: 异常类型简单类名, value: 分析函数(candidate, source, raw_stack, keywords) -> bool
_DEEP_ANALYZERS = {}


def _register_analyzer(exception_type):
    """装饰器：注册深度分析器"""
    def decorator(fn):
        _DEEP_ANALYZERS[exception_type] = fn
        return fn
    return decorator


def _deep_analyze_candidates(repo_path, candidates, raw_stack, keywords):
    """对 top-N 候选做源码级结构分析

    1. 查找已注册的异常类型专属分析器
    2. 回退到通用方法签名匹配
    """
    # 从异常类型选择分析器
    exception_type = ''
    m = re.search(r'(\w+(?:Exception|Error))', raw_stack or '')
    if m:
        exception_type = m.group(1)

    analyzer = _DEEP_ANALYZERS.get(exception_type)

    for candidate in candidates:
        file_path = candidate.file_path
        if not os.path.isabs(file_path):
            file_path = os.path.join(repo_path, file_path)
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                source = f.read()
        except OSError:
            continue

        # 异常专属分析器：命中则直接返回，未命中则跳过此候选
        if analyzer:
            if analyzer(candidate, source, raw_stack, keywords):
                return candidate
            continue  # 专属分析器明确否定，不用通用匹配覆盖

        # 无专属分析器时，用通用方法签名匹配
        if _match_method_signatures(source, keywords):
            logger.info('深度分析匹配: %s (方法签名含关键词)', candidate.class_name)
            return candidate

    return None


def _match_method_signatures(source, keywords):
    """检查方法签名中是否包含异常关键词"""
    if not keywords:
        return False
    # 匹配方法参数列表
    for m in re.finditer(r'(?:public|private|protected)\s+\S+\s+\w+\s*\(([^)]*)\)', source):
        params = m.group(1)
        for kw in keywords:
            if len(kw) >= 3 and kw in params:
                return True
    return False


# ── 异常专属深度分析器 ──────────────────────────────────────────

@_register_analyzer('MissingPathVariableException')
def _analyze_missing_path_variable(candidate, source, raw_stack, keywords):
    """检查 URL 模板变量与 @PathVariable 参数名是否匹配"""
    _PATH_TEMPLATE_RE = re.compile(r'\{(\w+)\}')
    _PATHVAR_ANNOTATION_RE = re.compile(
        r'@PathVariable\s*(?:\(\s*(?:value\s*=\s*)?["\']?(\w+)["\']?\s*\))?\s+(?:\w+\s+)?(\w+)')

    lines = source.splitlines()
    for i, line in enumerate(lines):
        if any(ann in line for ann in ['@GetMapping', '@PostMapping', '@PutMapping',
                                        '@DeleteMapping', '@RequestMapping']):
            template_vars = set(_PATH_TEMPLATE_RE.findall(line))
            for j in range(i, min(i + 4, len(lines))):
                for pm in _PATHVAR_ANNOTATION_RE.finditer(lines[j]):
                    bound_name = pm.group(1)
                    param_name = pm.group(2)
                    effective_name = bound_name if bound_name else param_name
                    if template_vars and effective_name not in template_vars:
                        logger.info('路径变量不匹配: %s 不在 %s，文件: %s',
                                    effective_name, template_vars, candidate.class_name)
                        return True
    return False


@_register_analyzer('NullPointerException')
def _analyze_npe(candidate, source, raw_stack, keywords):
    """检查源码中是否有可疑的链式调用（可能产生 NPE）"""
    # 从异常消息提取可能为 null 的变量
    null_var_re = re.compile(r'Cannot invoke "(\w+\.\w+)\(\)" because.*"(\w+)"')
    m = null_var_re.search(raw_stack or '')
    if m:
        method_chain = m.group(1)  # e.g. getAmount
        var_desc = m.group(2)
        # 检查源码中是否有未做 null 检查的链式调用
        if method_chain in source:
            return True
    return False


def _search_result_to_source_info(search_result, repo_path, file_io_mod):
    """将SearchResult转换为source_info格式"""
    try:
        file_path = search_result.file_path
        if not os.path.isabs(file_path):
            file_path = os.path.join(repo_path, file_path)

        source_text = file_io_mod.read_file(file_path)
        source_lines = source_text.splitlines()

        # 查找类定义行作为line_no
        class_name = search_result.class_name
        line_no = 0
        for i, line in enumerate(source_lines):
            if 'class %s' % class_name in line or 'interface %s' % class_name in line:
                line_no = i + 1
                break

        # 构建context_snippet
        if line_no > 0:
            start = max(0, line_no - 4)
            end = min(len(source_lines), line_no + 4)
            snippet = '\n'.join(source_lines[start:end])
        else:
            snippet = '\n'.join(source_lines[:10])

        return {
            'source_path': file_path,
            'repo_relative_path': os.path.relpath(file_path, repo_path),
            'class_name': search_result.class_name,
            'method': search_result.method_name or '',
            'line_no': line_no,
            'context_snippet': snippet,
            'full_source': source_text,
            'detection_method': 'graph_search',
        }
    except Exception as e:
        logger.debug('转换SearchResult失败: %s', e)
        return None


def _edit_code(config, llm_client, kw):
    raw_stack = kw.get('raw_stack', '')
    source_info = kw.get('source_info', {})

    # 验证 full_source 完整性：如果被截断，重新读取文件
    source_info = _ensure_full_source_complete(source_info, config.get('repo_path', ''))

    prompt = prompt_builder.build_prompt(config, raw_stack, source_info)
    max_tokens = int(config.get('max_tokens', 8192))
    patch_text = llm_client.generate_patch(prompt, max_tokens=max_tokens)
    return {'patch_text': patch_text, 'prompt': prompt}


def _ensure_full_source_complete(source_info, repo_path):
    """确保 source_info 中的 full_source 是完整的文件内容。

    如果 full_source 被截断（行数少于预期），重新读取文件获取完整内容。
    """
    if not source_info or not repo_path:
        return source_info

    full_source = source_info.get('full_source', '')
    if not full_source:
        return source_info

    # 检查 full_source 是否可能被截断
    source_lines = full_source.splitlines()
    if len(source_lines) < 10:
        # 文件太短，可能是截断的
        logger.warning('full_source 只有 %d 行，可能被截断', len(source_lines))
    elif not full_source.rstrip().endswith('}'):
        # 文件不以 } 结尾，可能被截断
        logger.warning('full_source 不以 } 结尾，可能被截断')
    else:
        # 检查括号平衡
        open_braces = full_source.count('{')
        close_braces = full_source.count('}')
        if open_braces > close_braces:
            logger.warning('full_source 括号不平衡（开括号 %d > 闭括号 %d），可能被截断',
                          open_braces, close_braces)
        else:
            # 看起来是完整的
            return source_info

    # 尝试重新读取文件
    repo_relative_path = source_info.get('repo_relative_path', '')
    if not repo_relative_path:
        return source_info

    source_path = os.path.join(repo_path, repo_relative_path.replace('\\', '/'))
    if not os.path.exists(source_path):
        logger.warning('源文件不存在: %s', source_path)
        return source_info

    try:
        with open(source_path, 'r', encoding='utf-8', errors='replace') as f:
            fresh_source = f.read()

        fresh_lines = fresh_source.splitlines()
        if len(fresh_lines) > len(source_lines):
            logger.info('重新读取源文件成功: %d 行 -> %d 行', len(source_lines), len(fresh_lines))
            source_info = source_info.copy()
            source_info['full_source'] = fresh_source

            # 同时更新 context_snippet
            line_no = source_info.get('line_no', 0)
            if line_no and line_no > 0:
                start = max(0, line_no - 4)
                end = min(len(fresh_lines), line_no + 4)
                source_info['context_snippet'] = '\n'.join(fresh_lines[start:end])
    except Exception as e:
        logger.warning('重新读取源文件失败: %s', e)

    return source_info


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
