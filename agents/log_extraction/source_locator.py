"""Source file location utilities for Java stack frames."""

import logging
import os
import re

logger = logging.getLogger(__name__)


def find_source_location(repo_path, frame, file_io, repo_graph=None):
    """Find source file and return context information for the top stack frame.

    Args:
        repo_path: Path to the repository root
        frame: Stack frame dict with class_name, method, line_no
        file_io: File I/O module
        repo_graph: Optional RepoGraph instance for enhanced search

    Returns:
        dict with source info or raises FileNotFoundError
    """
    class_name = frame.get('class_name', '')
    line_no = frame.get('line_no')
    repo_root = os.path.abspath(repo_path)

    rel_candidate = class_name.replace('.', os.sep) + '.java'
    candidates = [
        os.path.join(repo_root, 'src', 'main', 'java', rel_candidate),
        os.path.join(repo_root, 'src', 'test', 'java', rel_candidate),
        os.path.join(repo_root, rel_candidate),
    ]

    # 多模块项目：在子模块的 src 目录中搜索
    for src_dir in ['src/main/java', 'src/test/java']:
        for module_dir in os.listdir(repo_root):
            module_src = os.path.join(repo_root, module_dir, src_dir)
            if os.path.isdir(module_src):
                candidates.append(os.path.join(module_src, rel_candidate))

    source_path = None
    for candidate in candidates:
        if os.path.exists(candidate):
            source_path = candidate
            break

    if source_path is None:
        # Fallback: search by class basename
        simple_name = class_name.split('.')[-1] + '.java'
        for root, _, files in os.walk(repo_root):
            if simple_name in files:
                source_path = os.path.join(root, simple_name)
                break

    # OrcaLoca增强: 使用RepoGraph回退
    if source_path is None and repo_graph and repo_graph.is_built():
        logger.info(f"传统定位失败，尝试使用RepoGraph查找: {class_name}")
        source_path = _find_by_repo_graph(repo_graph, class_name, frame.get('method'))

    if source_path is None:
        raise FileNotFoundError('Could not locate source file for class: %s' % class_name)

    source_text = file_io.read_file(str(source_path))
    source_lines = source_text.splitlines()
    total_lines = len(source_lines)
    if line_no and line_no > 0:
        start = max(1, line_no - 3)
        end = min(total_lines, line_no + 3)
    else:
        start = 1
        end = min(total_lines, 20)

    snippet_lines = []
    for idx in range(start, end + 1):
        snippet_lines.append(source_lines[idx - 1])

    return {
        'source_path': source_path,
        'repo_relative_path': os.path.relpath(str(source_path), str(repo_root)),
        'class_name': class_name,
        'method': frame.get('method'),
        'line_no': line_no,
        'context_snippet': '\n'.join(snippet_lines),
        'full_source': source_text,
    }


def _find_by_repo_graph(repo_graph, class_name, method_name=None):
    """使用RepoGraph查找文件路径

    Args:
        repo_graph: RepoGraph实例
        class_name: 类名
        method_name: 方法名（可选）

    Returns:
        文件路径或None
    """
    # 尝试通过类名查找
    file_path = repo_graph.index.get_file_path(class_name)
    if file_path and os.path.exists(file_path):
        return file_path

    # 尝试通过类名+方法名查找
    if method_name:
        file_path = repo_graph.index.get_file_path(class_name, method_name)
        if file_path and os.path.exists(file_path):
            return file_path

    # 尝试查询类信息
    class_info = repo_graph.query_class(class_name)
    if class_info:
        file_path = class_info.get('file_path', '')
        if file_path and os.path.exists(file_path):
            return file_path

    return None


def select_best_frame(repo_path, parsed_stack, find_fn, repo_graph=None):
    """Select the best (most relevant) frame from the stack.

    Prioritizes frames where source files exist in the repo.
    Prefers application packages over framework packages.

    Args:
        repo_path: Path to the repository root
        parsed_stack: List of parsed stack frames
        find_fn: Function to find source location
        repo_graph: Optional RepoGraph for enhanced scoring

    Returns:
        Best frame dict or None
    """
    if not parsed_stack:
        return None

    # 收集所有可定位的帧
    valid_frames = []
    for frame in parsed_stack:
        try:
            info = find_fn(repo_path, frame, repo_graph=repo_graph)
            frame['_source_info'] = info
            valid_frames.append(frame)
        except (FileNotFoundError, OSError):
            continue

    if not valid_frames:
        return None

    if len(valid_frames) == 1:
        return valid_frames[0]

    # 对多个有效帧进行评分选择
    return _score_and_select_frame(valid_frames, repo_graph)


def _score_and_select_frame(frames, repo_graph=None):
    """对多个帧进行评分，选择最相关的

    评分标准:
    1. 应用代码优先于框架代码
    2. 有Spring注解的类优先
    3. 异常处理器优先
    4. 调用链中更靠近异常点的优先
    """
    if not frames:
        return None

    scored_frames = []
    for frame in frames:
        score = 0
        class_name = frame.get('class_name', '')

        # 1. 应用代码优先（非标准库和框架包）
        if not _is_framework_class(class_name):
            score += 100

        # 2. Spring注解加分
        source_info = frame.get('_source_info', {})
        if repo_graph and repo_graph.is_built():
            class_info = repo_graph.query_class(class_name)
            if class_info:
                annotations = class_info.get('annotations', [])
                # Spring Controller/Service/Repository 注解
                spring_annotations = ['@Controller', '@Service', '@Repository',
                                     '@RestController', '@Component', '@RestControllerAdvice']
                for ann in annotations:
                    if ann in spring_annotations:
                        score += 50
                    if ann == '@ExceptionHandler':
                        score += 80

        # 3. 帧位置（越靠前分数越高，但衰减）
        frame_index = frames.index(frame)
        score += max(0, 20 - frame_index * 5)

        scored_frames.append((score, frame))

    # 选择得分最高的帧
    scored_frames.sort(key=lambda x: x[0], reverse=True)
    return scored_frames[0][1]


def _is_framework_class(class_name):
    """判断是否是框架类（非应用代码）"""
    framework_prefixes = [
        'java.', 'javax.', 'sun.', 'com.sun.',
        'org.springframework.', 'org.apache.', 'org.slf4j.',
        'ch.qos.logback.', 'org.junit.', 'org.mockito.',
        'com.fasterxml.', 'org.hibernate.', 'org.eclipse.',
        'jakarta.', 'org.springframework.boot.',
        'org.springframework.web.', 'org.springframework.context.',
        'org.springframework.beans.', 'org.springframework.data.',
    ]
    return any(class_name.startswith(prefix) for prefix in framework_prefixes)


def locate_file_by_class_or_path(repo_path, class_name, file_path):
    """Locate file by class name or file path. Supports multi-module projects."""
    repo_root = os.path.abspath(repo_path)

    # Try file path first
    if file_path:
        candidate = os.path.join(repo_root, file_path.lstrip('/').lstrip('\\'))
        if os.path.exists(candidate):
            return candidate

    # Try class name
    if class_name:
        rel_candidate = class_name.replace('.', os.sep) + '.java'
        # 根目录 src/main/java 和 src/test/java
        candidates = [
            os.path.join(repo_root, 'src', 'main', 'java', rel_candidate),
            os.path.join(repo_root, 'src', 'test', 'java', rel_candidate),
        ]
        for cand in candidates:
            if os.path.exists(cand):
                return cand

        # 多模块项目：在子模块的 src/main/java 和 src/test/java 中搜索
        for src_dir in ['src/main/java', 'src/test/java']:
            for module_dir in os.listdir(repo_root):
                module_src = os.path.join(repo_root, module_dir, src_dir)
                if os.path.isdir(module_src):
                    candidate = os.path.join(module_src, rel_candidate)
                    if os.path.exists(candidate):
                        return candidate

        # 最终回退：按类名简单搜索
        simple_name = class_name.split('.')[-1] + '.java'
        for root, _, files in os.walk(repo_root):
            if simple_name in files:
                return os.path.join(root, simple_name)

    return None


def find_method_line(lines, method_name):
    """Find the line number of a method definition."""
    if not method_name:
        return None

    pattern = re.compile(r'(?:public|private|protected)?\s+\w+\s+' + re.escape(method_name) + r'\s*\(')
    for idx, line in enumerate(lines):
        if pattern.search(line):
            return idx + 1  # 1-based line number

    return None
