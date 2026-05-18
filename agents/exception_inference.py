"""Exception inference: LLM-based source location when stack has no app frames."""

import json
import logging
import os
import re

logger = logging.getLogger(__name__)


def scan_app_packages(repo_path):
    """Scan repo to identify main application packages."""
    packages = set()
    repo_root = os.path.abspath(repo_path)
    src_paths = [
        os.path.join(repo_root, 'src', 'main', 'java'),
        os.path.join(repo_root, 'src', 'main', 'kotlin'),
    ]

    for src_path in src_paths:
        if os.path.isdir(src_path):
            for root, dirs, files in os.walk(src_path):
                # Get package path
                rel = os.path.relpath(root, src_path)
                if rel != '.' and not rel.startswith('.'):
                    pkg = rel.replace(os.sep, '.').strip('.')
                    if pkg:
                        packages.add(pkg)

    return sorted(list(packages))[:10]


def build_inference_prompt(repo_path, raw_stack, app_packages):
    """Build a special prompt for LLM to infer source location from exception."""
    prompt = []
    prompt.append('## 任务：从异常消息反向定位源码位置')
    prompt.append('')
    prompt.append('你收到了一个 Java 异常，其堆栈主要是框架代码（Spring/Tomcat/Jakarta），')
    prompt.append('但真正的 BUG 在应用代码中。请根据异常消息分析并推断：')
    prompt.append('1. 最可能的源文件（完整路径或类名）')
    prompt.append('2. 最可能的方法名')
    prompt.append('3. 问题的简要说明')
    prompt.append('')
    prompt.append('## 项目结构')
    if app_packages:
        prompt.append('应用包名: %s' % ', '.join(app_packages[:5]))
    else:
        prompt.append('应用包名: (无法扫描，请自动推断)')
    prompt.append('')
    prompt.append('## 异常信息')
    prompt.append('```')
    prompt.append(raw_stack[:1200])
    prompt.append('```')
    prompt.append('')
    prompt.append('## 输出格式')
    prompt.append('返回 JSON:')
    prompt.append('```json')
    prompt.append('{')
    prompt.append('  "suspected_file": "src/main/java/com/fixflow/mall/api/MallController.java",')
    prompt.append('  "suspected_class": "com.fixflow.mall.api.MallController",')
    prompt.append('  "suspected_method": "getOrder",')
    prompt.append('  "reasoning": "MissingPathVariableException 说缺少 id 参数，likely @PathVariable 绑定错误"')
    prompt.append('}')
    prompt.append('```')
    prompt.append('')
    prompt.append('只返回 JSON，不要返回其他内容。')

    return '\n'.join(prompt)


def parse_inference_response(repo_path, llm_response, file_io, locate_fn):
    """Parse LLM's inference response and verify source location."""
    try:
        # Extract JSON from response
        if '```json' in llm_response:
            start = llm_response.find('{')
            end = llm_response.rfind('}') + 1
            if start >= 0 and end > start:
                json_str = llm_response[start:end]
            else:
                json_str = llm_response
        else:
            json_str = llm_response

        inferred = json.loads(json_str)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning('Could not parse LLM inference response: %s', llm_response[:200])
        return None

    suspected_file = inferred.get('suspected_file', '')
    suspected_class = inferred.get('suspected_class', '')
    suspected_method = inferred.get('suspected_method', '')
    reasoning = inferred.get('reasoning', '')

    # Try to locate the file
    source_path = locate_fn(repo_path, suspected_class, suspected_file)

    if not source_path or not os.path.exists(source_path):
        logger.warning('Could not verify inferred file: %s / %s', suspected_file, suspected_class)
        return None

    # Read file and extract context
    try:
        content = file_io.read_file(str(source_path))
        lines = content.splitlines()

        # Find method if possible
        from agents import source_locator
        method_line = source_locator.find_method_line(lines, suspected_method) if suspected_method else None
        if method_line is None:
            method_line = max(1, len(lines) // 2)  # Fallback to middle

        # Extract context around suspected line
        start = max(0, method_line - 4)
        end = min(len(lines), method_line + 10)
        snippet = '\n'.join(lines[start:end])

        repo_root = os.path.abspath(repo_path)
        rel_path = os.path.relpath(str(source_path), repo_root)

        return {
            'source_path': source_path,
            'repo_relative_path': rel_path,
            'class_name': suspected_class.split('.')[-1],
            'method': suspected_method,
            'line_no': method_line,
            'context_snippet': snippet,
            'full_source': content,
            'inferred': True,  # Mark as inferred, not from stack
            'reasoning': reasoning,
        }
    except Exception as e:
        logger.error('Error reading inferred file: %s', e)
        return None


def infer_from_exception_message(repo_path, raw_stack, parsed_stack,
                                  config, llm_client, file_io):
    """When stack has no app frames, let LLM infer the source location.

    Returns source_info dict or None if inference fails.
    """
    from agents import source_locator

    try:
        logger.info('Attempting to infer source location from exception message')

        # Build inference prompt
        app_packages = scan_app_packages(repo_path)
        inference_prompt = build_inference_prompt(repo_path, raw_stack, app_packages)

        max_tokens = int(config.get('max_tokens', 8192))
        llm_response = llm_client.generate_patch(inference_prompt, max_tokens=max_tokens)

        logger.debug('LLM inference response: %s', llm_response[:500])

        # Parse LLM response for suspected file path and method
        source_info = parse_inference_response(
            repo_path, llm_response, file_io, source_locator.locate_file_by_class_or_path
        )

        if source_info:
            logger.info('Successfully inferred source location: %s', source_info.get('repo_relative_path'))
        else:
            logger.warning('Failed to parse or verify inferred source location')

        return source_info
    except Exception as e:
        logger.error('Exception during source inference: %s', e)
        return None
