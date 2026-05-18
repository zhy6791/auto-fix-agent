"""LLM prompt construction for patch generation, test generation, and retries."""


def build_prompt(config, raw_stack, source_info):
    """Build the LLM prompt for minimal patch generation."""
    max_patch_lines = config.get('max_patch_lines', 40)
    prompt = []
    prompt.append('## 任务')
    prompt.append('根据以下 Java 异常堆栈和源码，生成一个最小化补丁以修复该异常。')
    prompt.append('')
    prompt.append('## 约束')
    prompt.append('- 修改最少（最多 %d 行）' % max_patch_lines)
    prompt.append('- 保证代码编译通过')
    prompt.append('- 不修改方法签名或删除业务逻辑')
    prompt.append('- 只修改上面给出的目标文件，且尽量只改问题行附近的局部代码')
    prompt.append('- 不要修改 package 声明、import 语句、类名、方法签名或文件路径，除非修复绝对依赖它们')
    prompt.append('- 不要猜测不存在的包名、类名或导入；如果不确定，返回 NO_SAFE_PATCH')
    prompt.append('- 优先使用 null check、边界检查等防御性编程')
    prompt.append('- 【重要】返回的 patched_content 必须是 COMPLETE 修复后的完整源文件，从 package 到最后一行')
    prompt.append('- 【重要】patched_content 必须可直接编译，不能包含任何代码片段、省略号(...)、行号前缀、或占位符')
    prompt.append('- 【重要】返回时保持原文件的 package、imports、类声明、所有方法完整')
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
    prompt.append('你的任务是修改上面的完整文件内容，只改动需要修复的地方，然后返回修改后的完整文件。')
    prompt.append('')
    prompt.append('返回以下格式之一:')
    prompt.append('')
    prompt.append('### 选项 1: JSON 格式（推荐）')
    prompt.append('```json')
    prompt.append('{')
    prompt.append('  "files": [')
    prompt.append('    {')
    prompt.append('      "path": "src/main/java/com/fixflow/mall/service/OrderService.java",')
    prompt.append('      "patched_content": "package com.fixflow.mall.service;\\n\\nimport ...;\\n\\npublic class OrderService {\\n  ...完整修改后的所有方法...\\n}"')
    prompt.append('    }')
    prompt.append('  ]')
    prompt.append('}')
    prompt.append('```')
    prompt.append('')
    prompt.append('### 选项 2: 无法安全修复')
    prompt.append('如果无法生成安全补丁，返回: NO_SAFE_PATCH: 原因说明')
    return '\n'.join(prompt)


def build_test_prompt(source_info, fix_patch_text):
    """Build LLM prompt for generating JUnit5 unit tests (Strategy B)."""
    lines = []
    lines.append('## 任务')
    lines.append('基于以下源码和修复内容，生成一个 JUnit5 单元测试类来验证修复的正确性。')
    lines.append('')
    lines.append('## 【重要】断言编写规则')
    lines.append('在编写测试断言前，你必须：')
    lines.append('1. **逐行阅读被测方法的源码**，理解每一行的实际行为')
    lines.append('2. **手动模拟执行**：对每个测试输入，按代码逻辑逐行推导输出值')
    lines.append('3. **基于推导结果编写断言**，不要凭直觉或方法名猜测返回值')
    lines.append('4. 特别注意：')
    lines.append('   - String.split() 返回的是数组，不是子串。"a:b:c".split(":")[1] 返回 "b"，不是 "b:c"')
    lines.append('   - 如果要获取第一个分隔符后的全部内容，应使用 substring(indexOf()+1) 而非 split()[1]')
    lines.append('   - 仔细区分方法的"实际行为"和"理想行为"，测试应验证实际行为')
    lines.append('')
    lines.append('## 要求')
    lines.append('- 测试框架：JUnit5（org.junit.jupiter.api）')
    lines.append('- 测试类名：以 "Test" 结尾（如 HelloControllerTest）')
    lines.append('- 生成 3-5 个测试用例，包括：')
    lines.append('  1. 异常复现：触发原异常的输入场景')
    lines.append('  2. 修复验证：验证修复后该场景不再报错')
    lines.append('  3. 边界值：null、空字符串、边界数值等')
    lines.append('  4. 回归测试：正常路径的功能验证')
    lines.append('- 使用 @Test、@DisplayName 等 JUnit5 注解')
    lines.append('- 必须使用 assertEquals、assertTrue、assertThrows 等断言')
    lines.append('- 完整的 package、imports、类声明、所有方法')
    lines.append('- 返回时保持原 package 和所有 imports 完整')
    lines.append('- 对于有依赖的方法（如 Repository、Service），使用 Mockito mock 外部依赖')
    lines.append('')
    lines.append('## 被修复的方法')
    lines.append('类名：%s' % source_info.get('class_name', 'Unknown'))
    lines.append('方法：%s' % source_info.get('method', 'unknown'))
    lines.append('文件：%s' % source_info.get('repo_relative_path', 'unknown'))
    lines.append('')
    lines.append('## 完整源码（请逐行阅读后再写测试）')
    lines.append('```java')
    full_source = source_info.get('full_source', '')
    if full_source:
        lines.append(full_source)
    else:
        lines.append(source_info.get('context_snippet', '(源码不可用)'))
    lines.append('```')
    lines.append('')
    lines.append('## 修复补丁内容（参考）')
    lines.append('```')
    lines.append(fix_patch_text[:1000])
    if len(fix_patch_text) > 1000:
        lines.append('... [补丁内容较长，摘要] ...')
    lines.append('```')
    lines.append('')
    lines.append('## 输出格式')
    lines.append('返回 JSON 格式：')
    lines.append('```json')
    lines.append('{')
    lines.append('  "files": [')
    lines.append('    {')
    lines.append('      "path": "src/test/java/包路径/类名Test.java",')
    lines.append('      "patched_content": "完整的测试类源码"')
    lines.append('    }')
    lines.append('  ]')
    lines.append('}')
    lines.append('```')
    lines.append('')
    lines.append('若无法生成安全测试，返回: NO_SAFE_PATCH: 原因说明')

    return '\n'.join(lines)


def generate_test_patch(source_info, fix_patch_text, config, llm_client):
    """Generate JUnit5 test patch via LLM."""
    test_prompt = build_test_prompt(source_info, fix_patch_text)
    max_tokens = int(config.get('max_tokens', 8192))
    test_patch_text = llm_client.generate_patch(test_prompt, max_tokens=max_tokens)
    return test_patch_text


def build_retry_prompt(original_prompt, source_info, failed_stage, error_output):
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
    lines.append('## 要求')
    lines.append('- 修复导致 %s 失败的问题' % stage_cn)
    lines.append('- 保持原来针对异常的正确修复不丢失')
    lines.append('- 返回格式与之前相同：JSON 格式的补丁')
    lines.append('- 如果无法同时满足编译和修复异常，优先保证编译通过')

    return '\n'.join(lines)
