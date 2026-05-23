"""自动生成 JUnit 5 单元测试模块。

在 apply_patch 和 commit 之后调用，为被修复的 Java 类生成回归测试，
验证修复是否正确解决了原始异常。
"""

import logging
import os
import re

from integrations.llm_client import _strip_markdown_fences
from agents.agent_loop.prompt_builder import _derive_test_path

logger = logging.getLogger(__name__)


def collect_project_context(repo_path, source_info, tools):
    """收集项目上下文信息，帮助 LLM 生成更准确的测试。

    Args:
        repo_path: 项目根目录。
        source_info: 源码信息字典。
        tools: 工具字典（file_io）。

    Returns:
        dict: 包含 pom_dependencies, existing_tests, dependency_sources, constructor_info 的字典。
    """
    context = {
        'pom_dependencies': '',
        'existing_tests': [],
        'dependency_sources': {},
        'constructor_info': '',
    }

    # 1. 读取 pom.xml 的依赖部分
    try:
        pom_path = os.path.join(repo_path, 'pom.xml')
        if os.path.exists(pom_path):
            pom_content = tools['file_io'].read_file(pom_path)
            # 提取 <dependencies> 部分
            dep_match = re.search(r'<dependencies>(.*?)</dependencies>', pom_content, re.DOTALL)
            if dep_match:
                context['pom_dependencies'] = dep_match.group(0)[:2000]
    except Exception as e:
        logger.debug('读取 pom.xml 失败: %s', e)

    # 2. 查找已有的测试文件作为示例
    try:
        test_dir = os.path.join(repo_path, 'src', 'test', 'java')
        if os.path.exists(test_dir):
            for root, dirs, files in os.walk(test_dir):
                for f in files:
                    if f.endswith('Test.java') and len(context['existing_tests']) < 2:
                        test_path = os.path.join(root, f)
                        try:
                            content = tools['file_io'].read_file(test_path)
                            # 只取前 100 行作为示例
                            lines = content.splitlines()[:100]
                            context['existing_tests'].append({
                                'path': os.path.relpath(test_path, repo_path).replace('\\', '/'),
                                'content': '\n'.join(lines),
                            })
                        except Exception:
                            pass
    except Exception as e:
        logger.debug('查找已有测试失败: %s', e)

    # 3. 读取被修复类的依赖类源码
    try:
        full_source = source_info.get('full_source', '')
        # 提取 import 语句，找到项目内的依赖类（支持任意层级包名）
        imports = re.findall(r'import\s+(com\.[a-zA-Z0-9_.]+);', full_source)
        # 优先读取返回类型和参数类型相关的类（更可能被测试用到）
        # 普通 import 最多读取 5 个
        for imp in imports[:5]:
            # 将包名转换为路径：最后一个点号前的是包路径，最后的是类名
            parts = imp.rsplit('.', 1)
            if len(parts) == 2:
                dep_path = parts[0].replace('.', '/') + '/' + parts[1] + '.java'
                dep_full_path = os.path.join(repo_path, 'src', 'main', 'java', dep_path)
                if os.path.exists(dep_full_path):
                    try:
                        content = tools['file_io'].read_file(dep_full_path)
                        # 类定义 + 方法签名部分通常够用，取前2000字符
                        context['dependency_sources'][imp] = content[:2000]
                    except Exception:
                        pass
    except Exception as e:
        logger.debug('读取依赖类失败: %s', e)

    # 4. 分析被测类的构造函数类型
    try:
        full_source = source_info.get('full_source', '')
        # 检查是否有带参构造函数
        constructor_match = re.search(
            r'public\s+\w+\(([^)]+)\)\s*\{',
            full_source
        )
        if constructor_match:
            params = constructor_match.group(1)
            context['constructor_info'] = (
                '该类使用构造函数注入，参数为: %s\n'
                '测试时需要使用 @BeforeEach 手动创建实例并传入 mock 对象，'
                '或者使用 MockitoAnnotations.openMocks(this) 配合 ReflectionTestUtils.setField'
            ) % params
        else:
            context['constructor_info'] = '该类有无参构造函数，可以使用 @InjectMocks 自动注入'
    except Exception as e:
        logger.debug('分析构造函数失败: %s', e)

    return context


def build_test_generation_prompt(source_info, patch_text, raw_stack,
                                 existing_test_content=None, project_context=None):
    """构建 LLM 测试生成 prompt。

    Args:
        source_info: 包含 class_name, method, line_no, repo_relative_path, full_source 的字典。
        patch_text: 已应用的 unified diff 补丁。
        raw_stack: 原始异常堆栈信息。
        existing_test_content: 已存在的测试文件内容，None 表示不存在。
        project_context: 项目上下文信息（pom_dependencies, existing_tests, dependency_sources）。

    Returns:
        str: 发送给 LLM 的 prompt。
    """
    lines = []
    lines.append('## 任务')
    lines.append('根据以下信息，为被修复的 Java 类生成 JUnit 5 单元测试。')
    lines.append('测试的目标是验证自动修复补丁是否正确地解决了原始异常。')
    lines.append('')

    lines.append('## 约束')
    lines.append('- 使用 JUnit 5 (Jupiter) 注解：@Test, @BeforeEach, @DisplayName 等')
    lines.append('- 【重要】对于依赖注入的字段（如 Repository、Service），必须使用 Mockito 的 @Mock 注解 + @ExtendWith(MockitoExtension.class)')
    lines.append('- 【重要】使用 @InjectMocks 注解注入被测类，用 @Mock 注解模拟所有依赖')
    lines.append('- 【重要】使用 when(...).thenReturn(...) 设置 mock 返回值，不要创建匿名实现类')
    lines.append('- 【禁止】不要创建接口的匿名实现类（如 new UserRepository() {...}），这会导致编译失败')
    lines.append('- 【禁止】不要使用 SpringBootTest，这是单元测试不是集成测试')
    lines.append('- 【禁止】不要调用依赖类中不存在的方法（如 result.isSuccess()），必须严格参考下面提供的依赖类源码中的实际方法')
    lines.append('- 【重要】对于 Result 类，使用 getCode() == 1 判断成功，不要使用 isSuccess()（该方法不存在）')
    lines.append('- 【重要】对于 RedisTemplate.delete()，使用 delete(any(Set.class)) 或 delete(anyCollection()）避免重载歧义')
    lines.append('- 测试方法命名清晰，使用 @DisplayName 说明测试意图')
    lines.append('- 至少包含以下测试用例：')
    lines.append('  1. 正向测试：验证修复后的方法在正常输入下能正确工作')
    lines.append('  2. 边界测试：验证原来导致异常的输入现在不再抛出异常')
    lines.append('  3. 如果适用，验证修复后的空值/null 处理')
    lines.append('- 测试必须是可编译的、完整的 Java 文件')
    lines.append('- 包含正确的 package 声明和 import 语句')
    lines.append('- 只使用项目中实际存在的类和方法，参考下面提供的依赖类源码')
    lines.append('')

    # 项目依赖信息
    if project_context and project_context.get('pom_dependencies'):
        lines.append('## 项目依赖 (pom.xml)')
        lines.append('```xml')
        lines.append(project_context['pom_dependencies'][:1500])
        lines.append('```')
        lines.append('')

    # 已有测试示例
    if project_context and project_context.get('existing_tests'):
        lines.append('## 已有测试文件示例（请参考其风格和配置）')
        for test in project_context['existing_tests']:
            lines.append('### %s' % test['path'])
            lines.append('```java')
            lines.append(test['content'])
            lines.append('```')
        lines.append('')

    # 依赖类源码
    if project_context and project_context.get('dependency_sources'):
        lines.append('## 被修复类的依赖类源码（mock 时需要参考这些接口）')
        for class_name, source in project_context['dependency_sources'].items():
            lines.append('### %s' % class_name)
            lines.append('```java')
            lines.append(source)
            lines.append('```')
        lines.append('')

    # 构造函数信息
    if project_context and project_context.get('constructor_info'):
        lines.append('## 构造函数注入信息')
        lines.append(project_context['constructor_info'])
        lines.append('')

    lines.append('## 原始异常信息')
    lines.append('```')
    lines.append(raw_stack[:1000])
    lines.append('```')
    lines.append('')

    lines.append('## 修复补丁 (unified diff)')
    lines.append('```diff')
    lines.append(patch_text[:3000])
    lines.append('```')
    lines.append('')

    class_name = source_info.get('class_name', 'Unknown')
    method = source_info.get('method', 'unknown')
    lines.append('## 被修复的类')
    lines.append('- 完整类名: %s' % class_name)
    lines.append('- 修复的方法: %s' % method)
    lines.append('- 源文件路径: %s' % source_info.get('repo_relative_path', ''))
    lines.append('')

    full_source = source_info.get('full_source', '')
    if full_source:
        source_lines = full_source.splitlines()
        lines.append('## 修复后的完整源码')
        lines.append('```java')
        if len(source_lines) > 200:
            lines.append('\n'.join(source_lines[:150]))
            lines.append('... [省略中间部分] ...')
            lines.append('\n'.join(source_lines[-50:]))
        else:
            lines.append(full_source)
        lines.append('```')
        lines.append('')

    if existing_test_content:
        lines.append('## 当前测试文件（已存在，需要合并）')
        lines.append('以下测试文件已经存在。请在保留所有已有测试方法的基础上，')
        lines.append('添加新的测试方法来验证本次修复。不要删除或修改已有的测试。')
        lines.append('```java')
        lines.append(existing_test_content)
        lines.append('```')
        lines.append('')

    # 编译错误信息（重试时提供）
    compile_error = source_info.get('compile_error', '')
    if compile_error:
        lines.append('## 上一次生成的测试代码编译失败')
        lines.append('请根据以下编译错误修正测试代码：')
        lines.append('```')
        lines.append(compile_error)
        lines.append('```')
        lines.append('')

    lines.append('## 输出要求')
    lines.append('- 只返回完整的 Java 测试类源码')
    lines.append('- 不要返回 markdown 围栏、JSON 或其他格式')
    lines.append('- 不要返回解释文字，只返回代码')
    lines.append('- 类名应为 %sTest' % class_name.split('.')[-1])
    lines.append('- 测试类的 package 应与被测类一致')
    lines.append('- 只 import 项目中实际存在的类，不要 import 不存在的类')

    return '\n'.join(lines)


def generate_test(llm_client, config, source_info, patch_text, raw_stack,
                  existing_test_content=None, project_context=None):
    """调用 LLM 生成 JUnit 测试代码。

    Args:
        llm_client: LLMClient 实例。
        config: 配置字典。
        source_info: 源码信息字典。
        patch_text: 已应用的补丁文本。
        raw_stack: 原始异常堆栈。
        existing_test_content: 已存在的测试文件内容。
        project_context: 项目上下文信息。

    Returns:
        str: Java 测试代码，失败时返回空字符串。
    """
    prompt = build_test_generation_prompt(
        source_info, patch_text, raw_stack,
        existing_test_content, project_context
    )

    max_tokens = int(config.get('test_gen_max_tokens', 4096))

    messages = [
        {
            'role': 'system',
            'content': (
                '你是一个专业的 Java 测试工程师。'
                '请生成高质量的 JUnit 5 单元测试代码。'
                '只返回可编译的 Java 源码，不要包含任何 markdown 标记或解释文字。'
                '只使用项目中实际存在的类和方法，不要凭空创造不存在的类。'
            ),
        },
        {'role': 'user', 'content': prompt},
    ]

    response = llm_client.chat(messages, max_tokens=max_tokens)

    if response.get('finish_reason') == 'error':
        logger.warning('LLM 测试生成失败: %s', response.get('content', ''))
        return ''

    test_code = response.get('content', '').strip()
    if not test_code:
        logger.warning('LLM 返回空的测试代码')
        return ''

    test_code = _strip_markdown_fences(test_code)

    if 'class ' not in test_code or '@Test' not in test_code:
        logger.warning('生成的代码不是有效的 JUnit 测试类')
        return ''

    return test_code


def write_test_file(repo_path, test_rel_path, test_code):
    """将测试代码写入文件。

    Args:
        repo_path: 项目根目录。
        test_rel_path: 测试文件的 repo 相对路径。
        test_code: Java 测试代码。

    Returns:
        str: 写入的绝对路径，失败时返回空字符串。
    """
    try:
        abs_path = os.path.join(repo_path, test_rel_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, 'w', encoding='utf-8') as f:
            f.write(test_code)
        logger.info('测试文件已写入: %s', abs_path)
        return abs_path
    except Exception as e:
        logger.error('写入测试文件失败: %s', e)
        return ''


def run_test_generation(config, repo_path, source_info, patch_text, raw_stack,
                        llm_client, tools):
    """执行完整的测试生成流程。

    1. 从 source_info 推导测试文件路径
    2. 收集项目上下文信息
    3. 读取已有测试文件（如存在）
    4. 调用 LLM 生成测试代码
    5. 写入测试文件
    6. 编译验证（失败时重试1次）
    7. 提交到 git

    Args:
        config: 配置字典。
        repo_path: 项目根目录。
        source_info: 源码信息字典。
        patch_text: 已应用的补丁文本。
        raw_stack: 原始异常堆栈。
        llm_client: LLMClient 实例。
        tools: 工具字典（file_io, git_manager）。

    Returns:
        dict: 包含 generated, test_path, committed, error 的结果字典。
    """
    result = {
        'generated': False,
        'test_path': '',
        'committed': False,
        'error': None,
    }

    try:
        test_rel_path = _derive_test_path(source_info)
        result['test_path'] = test_rel_path

        # 收集项目上下文
        logger.info('  收集项目上下文...')
        project_context = collect_project_context(repo_path, source_info, tools)

        # 读取已有测试文件
        test_abs_path = os.path.join(repo_path, test_rel_path)
        existing_content = None
        if os.path.exists(test_abs_path):
            try:
                existing_content = tools['file_io'].read_file(test_abs_path)
            except Exception:
                pass

        # 生成测试代码（最多重试1次）
        compile_error = None
        test_code = None
        for attempt in range(2):
            # 如果是重试，将编译错误加入 prompt
            if attempt == 1 and compile_error:
                logger.info('  编译失败，重试生成测试代码...')
                # 将编译错误追加到 source_info 供 prompt 使用
                source_info_with_error = dict(source_info)
                source_info_with_error['compile_error'] = compile_error
            else:
                source_info_with_error = source_info

            test_code = generate_test(
                llm_client, config, source_info_with_error, patch_text, raw_stack,
                existing_test_content=existing_content,
                project_context=project_context
            )
            if not test_code:
                result['error'] = 'LLM 未能生成有效的测试代码'
                return result

            # 写入文件
            written_path = write_test_file(repo_path, test_rel_path, test_code)
            if not written_path:
                result['error'] = '写入测试文件失败'
                return result

            # 编译验证
            if 'ci_pipeline' in tools and 'exec_cmd' in tools:
                try:
                    from agents.ci.ci_pipeline import run_compile
                    compile_result = run_compile(repo_path, config, tools['exec_cmd'])
                    if compile_result.get('code') != 0:
                        compile_error = compile_result.get('stderr', '') or compile_result.get('stdout', '')
                        # 提取关键错误信息（最多500字符）
                        if compile_error:
                            lines = compile_error.splitlines()
                            error_lines = [l for l in lines if 'ERROR' in l or 'error' in l.lower()]
                            compile_error = '\n'.join(error_lines[:10])[:500]
                        logger.warning('  测试编译失败: %s', compile_error[:200])
                        continue  # 重试
                except Exception as e:
                    logger.debug('编译验证跳过: %s', e)

            # 编译通过或跳过验证
            break

        result['generated'] = True

        # 提交
        commit_msg = 'test: add JUnit tests for auto-fix in %s' % (
            source_info.get('class_name', 'unknown')
        )
        committed = tools['git_manager'].commit_changes(
            repo_path, commit_msg, files=[test_rel_path]
        )
        result['committed'] = committed

    except Exception as e:
        logger.exception('测试生成流程失败')
        result['error'] = str(e)

    return result
