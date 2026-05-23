"""ReAct agent loop for autonomous bug location and patch generation.

The agent runs a Thought → Action → Observation loop, calling tools
to locate source code and generate patches. It exits when the agent
calls final_patch (success) or abort (failure).
"""

import json
import logging
import re

logger = logging.getLogger(__name__)


class ReActAgent:
    """LLM-driven decision loop for the locate-and-fix phase."""

    def __init__(self, config, tool_registry, llm_client, max_iterations=10):
        self.config = config
        self.tool_registry = tool_registry
        self.llm_client = llm_client
        self.max_iterations = max_iterations
        self.scratchpad = []
        self.thoughts = []
        self.tool_call_history = []

    def run(self, context):
        """Execute the ReAct loop.

        Args:
            context: dict with keys:
                - raw_stack: str (extracted exception block)
                - parsed_stack: list (parsed stack frames)
                - repo_path: str
                - dry_run: bool

        Returns:
            dict with keys:
                - final_patch: str or None
                - source_info: dict or None
                - aborted: bool
                - abort_reason: str
                - iterations: int
                - thoughts: list
                - tool_calls: list
        """
        self.scratchpad = []
        self.thoughts = []
        self.tool_call_history = []

        system_prompt = self._build_system_prompt()
        user_message = self._build_initial_user_message(context)

        self.scratchpad.append({'role': 'system', 'content': system_prompt})
        self.scratchpad.append({'role': 'user', 'content': user_message})

        result = {
            'final_patch': None,
            'source_info': None,
            'aborted': False,
            'abort_reason': '',
            'iterations': 0,
            'thoughts': self.thoughts,
            'tool_calls': self.tool_call_history,
        }

        for iteration in range(self.max_iterations):
            result['iterations'] = iteration + 1
            logger.info('  ┌── 迭代 %d/%d ──────────────────────────────────', iteration + 1, self.max_iterations)

            # Call LLM
            llm_response = self._call_llm()
            if llm_response is None:
                result['aborted'] = True
                result['abort_reason'] = 'LLM call failed'
                return result

            # Parse response
            thought, tool_name, tool_args = self._parse_response(llm_response)
            if thought:
                self.thoughts.append(thought)
                logger.info('  │ 💭 思考: %s', thought[:150])

            if tool_name is None:
                # Couldn't parse any action, ask agent to clarify
                self.scratchpad.append({'role': 'assistant', 'content': llm_response.get('content', '')})
                self.scratchpad.append({'role': 'user', 'content': 'Observation: Could not parse your action. Please respond with Thought: followed by Action: tool_name({args})'})
                continue

            # Format args concisely for logging
            arg_parts = []
            for k, v in tool_args.items():
                if k in ('patch_text', 'raw_stack', 'full_source'):
                    continue
                val_str = str(v)
                if len(val_str) > 50:
                    val_str = val_str[:47] + '...'
                arg_parts.append('%s=%s' % (k, val_str))
            args_display = ', '.join(arg_parts)
            logger.info('  │ 🔧 行动: [%s] %s', tool_name, args_display)

            # Handle signal tools
            if tool_name == 'final_patch':
                logger.info('  │ ✅ 完成: 补丁已提交，退出Agent循环')
                logger.info('  └────────────────────────────────────────────')
                result['final_patch'] = tool_args.get('patch_text', '')
                result['source_info'] = tool_args.get('source_info', {})
                result['test_code'] = tool_args.get('test_code', '')
                self.tool_call_history.append({'tool': tool_name, 'args': tool_args, 'result': 'success'})
                return result

            if tool_name == 'abort':
                logger.info('  │ ❌ 中止: %s', tool_args.get('reason', 'Unknown'))
                logger.info('  └────────────────────────────────────────────')
                result['aborted'] = True
                result['abort_reason'] = tool_args.get('reason', 'Unknown')
                self.tool_call_history.append({'tool': tool_name, 'args': tool_args, 'result': 'aborted'})
                return result

            # Execute tool
            tool_result = self.tool_registry.execute(tool_name, tool_args)
            tool_result_str = self._format_observation(tool_result)
            logger.info('  │ 📋 结果: %s', tool_result_str[:200])
            logger.info('  └────────────────────────────────────────────')

            # Update scratchpad
            assistant_msg = 'Thought: %s\nAction: %s(%s)' % (thought or '', tool_name, json.dumps(tool_args, ensure_ascii=False))
            self.scratchpad.append({'role': 'assistant', 'content': assistant_msg})
            self.scratchpad.append({'role': 'user', 'content': 'Observation: %s' % tool_result_str})

            self.tool_call_history.append({
                'tool': tool_name,
                'args': tool_args,
                'result': tool_result_str[:500],
            })

            # Manage context window
            self._manage_context_window(system_prompt)

        # Max iterations reached
        logger.warning('Agent reached max iterations (%d)', self.max_iterations)
        result['aborted'] = True
        result['abort_reason'] = 'Reached maximum iterations (%d)' % self.max_iterations
        return result

    def _build_system_prompt(self):
        max_patch_lines = self.config.get('max_patch_lines', 40)
        max_patch_hunks = self.config.get('max_patch_hunks', 3)
        tool_descriptions = self.tool_registry.get_text_tool_descriptions()

        # 基础工作流程
        workflow = '''## 工作流程

### 场景A：堆栈包含应用代码帧
1. 分析异常堆栈，识别应用层代码帧（非 java.*/javax.*/org.springframework.* 等框架包）
2. 使用 locate_from_stack 定位源文件
3. 阅读源码，分析 BUG 根因
4. 使用 edit_code 生成修复补丁
5. 使用 validate_patch 校验补丁安全性
6. 校验通过后，使用 generate_test 生成 JUnit 单元测试
7. 最后调用 final_patch 提交最终补丁和测试代码

### 场景B：堆栈全部是框架代码（Spring/Tomcat/JDK等）
**禁止行为：**
- 禁止使用 locate_from_stack（会失败）
- 禁止使用 search_code 猜测类名（会浪费迭代）
- 禁止猜测包名前缀（如 com.example.*）

**必须按以下顺序执行：**

步骤1：调用 infer_source 定位源文件
   - 示例：infer_source({"raw_stack": "...", "parsed_stack": [...]})
   - 该工具内部会先搜索代码图谱，再用LLM推断，准确率高

步骤2：用 read_code 读取 infer_source 返回的 source_path

步骤3：分析源码，用 edit_code 生成补丁

步骤4：用 validate_patch 校验，generate_test 生成测试，final_patch 提交'''

        # OrcaLoca增强已在工作流程中直接体现，无需额外追加

        return '''你是一个专业的 Java Web 服务自动调试和修复助手。你的任务是分析 Java 异常日志，定位源码中的 BUG，生成修复补丁和单元测试。

%s

## 约束
- 补丁必须是 unified diff 格式
- 修改尽量少（最多 %d 行）
- 每个文件最多 %d 个 hunk
- 不修改 package 声明、import 语句、类名、方法签名（除非绝对必要）
- 不删除文件头部注释、版权注释、Javadoc、TODO/FIXME
- 如果无法安全修复，调用 abort 并说明原因
- 每次只调用一个工具

## 可用工具
%s

## 响应格式
每次响应必须包含：
1. Thought: 你的分析和下一步计划
2. Action: 要调用的工具名称和参数

示例1（堆栈有应用代码帧）：
Thought: 堆栈第一帧是 HelloController.sayHello，先定位源文件。
Action: locate_from_stack({"class_name": "com.example.HelloController", "method": "sayHello", "line_no": 42})

示例2（堆栈全是框架代码）：
Thought: 堆栈全是 Spring 框架代码，无法直接定位。使用 infer_source 通过代码图谱搜索定位应用代码。
Action: infer_source({"raw_stack": "...", "parsed_stack": [...]})
''' % (workflow, max_patch_lines, max_patch_hunks, tool_descriptions)

    def _build_initial_user_message(self, context):
        raw_stack = context.get('raw_stack', '')
        parsed_stack = context.get('parsed_stack', [])
        repo_path = context.get('repo_path', '')

        lines = []
        lines.append('## 异常信息')
        lines.append('```')
        lines.append(raw_stack[:2000])
        lines.append('```')
        lines.append('')
        lines.append('## 已解析的堆栈帧')
        lines.append('```json')
        lines.append(json.dumps(parsed_stack[:20], ensure_ascii=False, indent=2))
        lines.append('```')
        lines.append('')
        lines.append('## 仓库路径')
        lines.append(repo_path)
        lines.append('')
        lines.append('请分析异常，定位源码中的 BUG，生成修复补丁。完成后调用 final_patch。')

        return '\n'.join(lines)

    def _call_llm(self):
        tools_schema = self.tool_registry.get_openai_tool_schemas()
        try:
            response = self.llm_client.chat(
                messages=self.scratchpad,
                tools=tools_schema,
                max_tokens=int(self.config.get('max_tokens', 4096)),
            )
            return response
        except Exception as e:
            logger.exception('LLM call failed in agent loop')
            return None

    def _parse_response(self, llm_response):
        """Parse LLM response into (thought, tool_name, tool_args)."""
        content = llm_response.get('content', '')
        tool_calls = llm_response.get('tool_calls')

        # Mode A: function calling
        if tool_calls and len(tool_calls) > 0:
            tc = tool_calls[0]
            tool_name = tc.get('name', '')
            tool_args = tc.get('arguments', {})
            # Extract thought from content if present
            thought = ''
            if content:
                thought_match = re.search(r'Thought:\s*(.+?)(?=Action:|$)', content, re.DOTALL)
                if thought_match:
                    thought = thought_match.group(1).strip()
                else:
                    thought = content.strip()
            return thought, tool_name, tool_args

        # Mode B: text-based parsing
        if not content:
            return '', None, {}

        # Extract thought
        thought = ''
        thought_match = re.search(r'Thought:\s*(.+?)(?=Action:|$)', content, re.DOTALL)
        if thought_match:
            thought = thought_match.group(1).strip()

        # Extract action
        action_match = re.search(r'Action:\s*(\w+)\((.*?)\)\s*$', content, re.DOTALL | re.MULTILINE)
        if action_match:
            tool_name = action_match.group(1)
            args_str = action_match.group(2).strip()
            try:
                tool_args = json.loads(args_str) if args_str else {}
            except json.JSONDecodeError:
                tool_args = {}
            return thought, tool_name, tool_args

        # Try JSON block
        json_match = re.search(r'```json\s*(\{.*?\})\s*```', content, re.DOTALL)
        if json_match:
            try:
                obj = json.loads(json_match.group(1))
                if 'tool' in obj:
                    return thought, obj['tool'], obj.get('arguments', {})
            except (json.JSONDecodeError, KeyError):
                pass

        return thought, None, {}

    def _format_observation(self, tool_result):
        """Format tool result as observation string for the scratchpad."""
        if isinstance(tool_result, str):
            return tool_result
        try:
            return json.dumps(tool_result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(tool_result)

    def _manage_context_window(self, system_prompt):
        """Compress old observations if scratchpad grows too large.

        Keeps system prompt + initial user message + last 10 full exchanges.
        Older exchanges are summarized.
        """
        # 2 (system + initial user) + N*2 (assistant + observation) pairs
        max_messages = 2 + 10 * 2  # keep last 10 exchanges
        if len(self.scratchpad) <= max_messages:
            return

        # Keep: system prompt (idx 0), initial user (idx 1), last 20 messages
        preserved = self.scratchpad[:2] + self.scratchpad[-(max_messages - 2):]

        # Build summary of removed messages
        removed = self.scratchpad[2:-(max_messages - 2)]
        summary_parts = []
        for msg in removed:
            content = msg.get('content', '')
            if content.startswith('Thought:'):
                summary_parts.append(content[:100])
            elif content.startswith('Observation:'):
                summary_parts.append('Observation: [compressed] (%d chars)' % len(content))

        if summary_parts:
            summary_msg = {
                'role': 'user',
                'content': '[Summary of earlier iterations]\n%s' % '\n'.join(summary_parts[-5:]),
            }
            self.scratchpad = preserved[:2] + [summary_msg] + preserved[2:]
        else:
            self.scratchpad = preserved
