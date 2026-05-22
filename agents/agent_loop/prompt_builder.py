"""LLM prompt construction for patch generation, test generation, and retries."""


def _derive_test_path(source_info):
    """从源码路径推导测试文件路径，兼容 Windows 反斜杠。"""
    repo_rel = source_info.get('repo_relative_path', '')
    repo_rel = repo_rel.replace('\\', '/')
    test_path = repo_rel.replace('/main/java/', '/test/java/')
    if not test_path.rsplit('/', 1)[-1].endswith('Test.java'):
        test_path = test_path.replace('.java', 'Test.java')
    return test_path


def build_prompt(config, raw_stack, source_info):
    """Build the LLM prompt for minimal patch generation."""
    max_patch_lines = config.get('max_patch_lines', 40)
    max_patch_hunks = config.get('max_patch_hunks', 3)
    prompt = []
    prompt.append('## 任务')
    prompt.append('根据以下 Java 异常堆栈和源码，生成一个最小化补丁以修复该异常。')
    prompt.append('')
    prompt.append('## 约束')
    prompt.append('- 修改最少（最多 %d 行）' % max_patch_lines)
    prompt.append('- 每个文件最多 %d 个 hunk，且每个 hunk 只能修改局部区域' % max_patch_hunks)
    prompt.append('- 保证代码编译通过')
    prompt.append('- 不修改方法签名或删除业务逻辑')
    prompt.append('- 只修改上面给出的目标文件，且尽量只改问题行附近的局部代码')
    prompt.append('- 不要修改 package 声明、import 语句、类名、方法签名或文件路径，除非修复绝对依赖它们')
    prompt.append('- 不要猜测不存在的包名、类名或导入；如果不确定，返回 NO_SAFE_PATCH')
    prompt.append('- 优先使用 null check、边界检查等防御性编程')
    prompt.append('- 【重要】只允许返回 unified diff，不允许返回 JSON、patched_content、整文件重写或代码片段替换式补丁')
    prompt.append('- 【重要】diff 必须尽量只包含少量局部 hunks，不允许大范围替换整个文件')
    prompt.append('- 【重要】不得删除文件头部注释、版权注释、LICENSE/Javadoc/TODO/FIXME 等关键注释，除非修复绝对依赖且在 diff 中明确保留')
    prompt.append('')

    # If this is an inferred source (not from stack trace), add extra context
    if source_info.get('inferred'):
        prompt.append('## 重要提示')
        prompt.append('本次异常是基于异常消息推断定位的源码位置，而不是从堆栈直接追踪。')
        prompt.append('推断理由：%s' % source_info.get('reasoning', 'N/A'))
        prompt.append('请特别注意：生成的补丁应该符合推断的问题描述，确保修复与异常消息一致。')
        prompt.append('')
    prompt.append('')
    prompt.append('## 异常信息')
    prompt.append('```')
    prompt.append(raw_stack[:500])  # Limit stack size
    prompt.append('```')
    prompt.append('')
    prompt.append('## 源码位置与问题上下文')
    prompt.append('文件: %s (repo 相对路径)' % source_info.get('repo_relative_path'))
    prompt.append('问题行: %s' % source_info.get('line_no'))
    prompt.append('方法: %s' % source_info.get('method'))
    prompt.append('')
    prompt.append('### 问题行附近代码（7-8 行窗口）:')
    prompt.append('```java')
    prompt.append(source_info.get('context_snippet', ''))
    prompt.append('```')
    prompt.append('')

    # Add complete file content
    full_source = source_info.get('full_source', '')
    if full_source:
        prompt.append('### 原始完整文件内容（作为修改基础）:')
        prompt.append('```java')
        lines = full_source.splitlines()
        if len(lines) > 150:
            prompt.append('\n'.join(lines[:100]))
            prompt.append('... [中间部分省略] ...')
            prompt.append('\n'.join(lines[-50:]))
        else:
            prompt.append(full_source)
        prompt.append('```')
        prompt.append('')

    prompt.append('## 输出格式说明')
    prompt.append('你的任务是直接返回 unified diff（--- / +++ / @@ / + / - /  行），只修改必要的局部代码。')
    prompt.append('')
    prompt.append('返回以下格式之一:')
    prompt.append('')
    prompt.append('### 选项 1: unified diff')
    prompt.append('```diff')
    prompt.append('--- a/src/main/java/com/fixflow/mall/service/OrderService.java')
    prompt.append('+++ b/src/main/java/com/fixflow/mall/service/OrderService.java')
    prompt.append('@@ -1,3 +1,3 @@')
    prompt.append('-old line')
    prompt.append('+new line')
    prompt.append('```')
    prompt.append('')
    prompt.append('### 选项 2: 无法安全修复')
    prompt.append('如果无法生成安全补丁，返回: NO_SAFE_PATCH: 原因说明')
    return '\n'.join(prompt)


def build_retry_prompt(original_prompt, source_info, failed_stage, error_output,
                       broken_file_content=None):
    """Build a prompt asking the LLM to fix compile/test failures."""
    stage_cn = '编译' if failed_stage == 'compile' else '单元测试'

    lines = []
    lines.append('## 修复任务：你的上一个补丁导致了%s失败' % stage_cn)
    lines.append('')
    lines.append('请分析下面的错误输出，并生成一个新的修复补丁。')
    lines.append('')
    lines.append('## 原始修复任务（上下文）')
    lines.append(original_prompt)
    lines.append('')
    lines.append('## %s 错误输出' % stage_cn)
    lines.append('```')
    lines.append(error_output)
    lines.append('```')
    lines.append('')

    if broken_file_content:
        lines.append('## 当前失败的文件完整内容（请基于此内容生成修复补丁）')
        lines.append('```java')
        lines.append(broken_file_content)
        lines.append('```')
        lines.append('')

    lines.append('## 要求')
    lines.append('- 修复导致 %s 失败的问题' % stage_cn)
    lines.append('- 保持原来针对异常的正确修复不丢失')
    lines.append('- 【重要】只返回 unified diff 格式的补丁，不要包含 ```diff 或 ``` 围栏标记')
    lines.append('- 【重要】补丁中的代码必须是有效的 Java 代码，不能包含 markdown 标记')
    lines.append('- 如果无法同时满足编译和修复异常，优先保证编译通过')

    return '\n'.join(lines)
