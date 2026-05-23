"""
搜索管理器 - 提供6种结构化搜索策略
"""
import logging
from pathlib import Path
from typing import Dict, List, Optional

from agents.code_graph.repo_graph import RepoGraph
from tools.file_io import read_file

logger = logging.getLogger(__name__)


class SearchResult:
    """搜索结果"""

    def __init__(self, file_path: str, class_name: str = '', method_name: str = '',
                 line_no: int = 0, context_snippet: str = '', score_hint: float = 0.0):
        self.file_path = file_path
        self.class_name = class_name
        self.method_name = method_name
        self.line_no = line_no
        self.context_snippet = context_snippet
        self.score_hint = score_hint

    def to_dict(self) -> Dict:
        return {
            'file_path': self.file_path,
            'class_name': self.class_name,
            'method_name': self.method_name,
            'line_no': self.line_no,
            'context_snippet': self.context_snippet,
            'score_hint': self.score_hint
        }


class SearchManager:
    """
    搜索管理器，提供6种搜索策略

    搜索策略:
    1. search_by_exception_type - 按异常类型查找相关类/方法
    2. search_by_class_name - 按类名查找（增强版source_locator）
    3. search_by_method_name - 按方法名跨代码库搜索
    4. search_by_annotation - 按Spring注解查找
    5. search_by_import_pattern - 按import模式查找
    6. search_by_stack_context - 多帧分析，结合调用图扩展搜索
    """

    def __init__(self, repo_graph: RepoGraph, repo_path: str):
        self.graph = repo_graph
        self.repo_path = Path(repo_path)

    def search_by_exception_type(self, exception_type: str, max_results: int = 10) -> List[SearchResult]:
        """
        按异常类型搜索相关类/方法

        Args:
            exception_type: 异常类型名称
            max_results: 最大结果数

        Returns:
            搜索结果列表
        """
        results = []

        # 从倒排索引搜索
        matches = self.graph.index.search_by_exception(exception_type)
        for class_name, method_name, file_path in matches[:max_results]:
            context = self._get_method_context(file_path, class_name, method_name)
            results.append(SearchResult(
                file_path=file_path,
                class_name=class_name,
                method_name=method_name,
                context_snippet=context,
                score_hint=80.0  # 异常类型匹配给高分
            ))

        # 从图搜索（throws关系）
        if not results:
            exc_methods = self.graph.get_exception_flow(exception_type)
            for method_info in exc_methods[:max_results]:
                file_path = method_info.get('file_path', '')
                class_name = method_info.get('class_name', '')
                method_name = method_info.get('name', '')
                context = self._get_method_context(file_path, class_name, method_name)
                results.append(SearchResult(
                    file_path=file_path,
                    class_name=class_name,
                    method_name=method_name,
                    context_snippet=context,
                    score_hint=75.0
                ))

        # 框架异常回退：如果异常类型来自Spring Web框架，搜索Controller类
        if not results and self._is_spring_web_exception(exception_type):
            logger.info('框架异常 %s 无直接匹配，回退到Controller搜索', exception_type)
            for ann in ['@RestController', '@Controller', '@RestControllerAdvice']:
                ann_results = self.search_by_annotation(ann, max_results)
                logger.info('  注解 %s 匹配: %d 个类', ann, len(ann_results))
                for r in ann_results:
                    r.score_hint = 60.0
                results.extend(ann_results)
            results = results[:max_results]

        logger.info('search_by_exception_type(%s) 最终返回 %d 个候选', exception_type, len(results))
        return results

    @staticmethod
    def _is_spring_web_exception(exception_type: str) -> bool:
        """判断是否是Spring Web框架异常"""
        spring_web_exceptions = [
            'MissingPathVariableException', 'MissingServletRequestParameterException',
            'TypeMismatchException', 'MethodArgumentNotValidException',
            'HttpRequestMethodNotSupportedException', 'HttpMediaTypeNotSupportedException',
            'HttpMessageNotReadableException', 'NoHandlerFoundException',
            'AsyncRequestTimeoutException', 'ConversionNotSupportedException',
            'HttpMessageNotWritableException', 'MissingMatrixVariableException',
            'MissingRequestCookieException', 'MissingRequestHeaderException',
            'ServletRequestBindingException', 'BindException',
        ]
        if exception_type in spring_web_exceptions:
            return True
        return exception_type.startswith('org.springframework.')

    def search_by_class_name(self, class_name: str, max_results: int = 10) -> List[SearchResult]:
        """
        按类名搜索文件

        Args:
            class_name: 类名（简单类名或全限定名）
            max_results: 最大结果数

        Returns:
            搜索结果列表
        """
        results = []

        # 从倒排索引搜索
        file_paths = self.graph.index.search_by_class(class_name)
        for file_path in file_paths[:max_results]:
            # 读取文件头部获取package信息
            context = self._get_class_context(file_path, class_name)
            results.append(SearchResult(
                file_path=file_path,
                class_name=class_name,
                context_snippet=context,
                score_hint=90.0  # 类名精确匹配给最高分
            ))

        # 如果没有找到，尝试模糊匹配
        if not results:
            results = self._fuzzy_search_class(class_name, max_results)

        return results

    def search_by_method_name(self, method_name: str, max_results: int = 10) -> List[SearchResult]:
        """
        按方法名搜索

        Args:
            method_name: 方法名
            max_results: 最大结果数

        Returns:
            搜索结果列表
        """
        results = []

        # 从倒排索引搜索
        matches = self.graph.index.search_by_method(method_name)
        for class_name, file_path, line_no in matches[:max_results]:
            context = self._get_method_context(file_path, class_name, method_name)
            results.append(SearchResult(
                file_path=file_path,
                class_name=class_name,
                method_name=method_name,
                line_no=line_no,
                context_snippet=context,
                score_hint=70.0  # 方法名匹配
            ))

        return results

    def search_by_annotation(self, annotation: str, max_results: int = 10) -> List[SearchResult]:
        """
        按注解搜索（如@Service, @Controller, @ExceptionHandler）

        Args:
            annotation: 注解名称
            max_results: 最大结果数

        Returns:
            搜索结果列表
        """
        results = []

        # 从倒排索引搜索
        matches = self.graph.index.search_by_annotation(annotation)
        for class_name, file_path in matches[:max_results]:
            context = self._get_class_context(file_path, class_name)
            results.append(SearchResult(
                file_path=file_path,
                class_name=class_name,
                context_snippet=context,
                score_hint=85.0  # 注解匹配给高分，因为Spring注解通常是关键位置
            ))

        return results

    def search_by_import_pattern(self, import_pattern: str, max_results: int = 10) -> List[SearchResult]:
        """
        按import模式搜索

        Args:
            import_pattern: import模式（如com.example.service）
            max_results: 最大结果数

        Returns:
            搜索结果列表
        """
        results = []

        # 遍历所有文件查找匹配的import
        for file_path, file_info in self.graph._file_cache.items():
            if len(results) >= max_results:
                break

            imports = file_info.get('imports', [])
            matching_imports = [imp for imp in imports if import_pattern in imp]

            if matching_imports:
                for cls in file_info.get('classes', []):
                    class_name = cls['name']
                    context = self._get_class_context(file_path, class_name)
                    results.append(SearchResult(
                        file_path=file_path,
                        class_name=class_name,
                        context_snippet=context,
                        score_hint=60.0  # import模式匹配给中等分
                    ))
                    break  # 每个文件只返回一个结果

        return results

    def search_by_stack_context(self, parsed_stack: List[Dict], max_results: int = 10) -> List[SearchResult]:
        """
        多帧分析，结合调用图扩展搜索

        Args:
            parsed_stack: 解析后的堆栈帧列表
            max_results: 最大结果数

        Returns:
            搜索结果列表
        """
        results = []
        seen_files = set()

        # 1. 首先尝试直接定位堆栈帧中的应用代码
        for frame in parsed_stack:
            class_name = frame.get('class_name', '')
            method_name = frame.get('method', '')
            file_path = frame.get('source_file', '')

            # 尝试直接查找文件
            if file_path:
                full_path = self._find_file_by_name(file_path)
                if full_path and full_path not in seen_files:
                    seen_files.add(full_path)
                    context = self._get_method_context(full_path, class_name, method_name)
                    results.append(SearchResult(
                        file_path=full_path,
                        class_name=class_name,
                        method_name=method_name,
                        line_no=frame.get('line_no', 0),
                        context_snippet=context,
                        score_hint=95.0  # 堆栈帧直接匹配给最高分
                    ))

            # 尝试通过类名查找
            if class_name and len(results) < max_results:
                file_paths = self.graph.index.search_by_class(class_name)
                for fp in file_paths:
                    if fp not in seen_files:
                        seen_files.add(fp)
                        context = self._get_method_context(fp, class_name, method_name)
                        results.append(SearchResult(
                            file_path=fp,
                            class_name=class_name,
                            method_name=method_name,
                            line_no=frame.get('line_no', 0),
                            context_snippet=context,
                            score_hint=85.0
                        ))

        # 2. 如果结果不足，通过调用图扩展搜索
        if len(results) < max_results:
            for result in results[:]:  # 遍历当前结果的副本
                if len(results) >= max_results:
                    break

                # 获取调用者
                callers = self.graph.get_callers(result.class_name, result.method_name)
                for caller in callers:
                    if len(results) >= max_results:
                        break

                    caller_file = caller.get('file_path', '')
                    caller_class = caller.get('class_name', '')
                    caller_method = caller.get('name', '')

                    if caller_file and caller_file not in seen_files:
                        seen_files.add(caller_file)
                        context = self._get_method_context(caller_file, caller_class, caller_method)
                        results.append(SearchResult(
                            file_path=caller_file,
                            class_name=caller_class,
                            method_name=caller_method,
                            context_snippet=context,
                            score_hint=65.0  # 调用者给较低分
                        ))

        return results

    def _find_file_by_name(self, file_name: str) -> Optional[str]:
        """根据文件名查找完整路径"""
        # 直接路径
        if Path(file_name).exists():
            return file_name

        # 在仓库中搜索
        for root, dirs, files in os.walk(self.repo_path):
            if file_name in files:
                return str(Path(root) / file_name)

        return None

    def _get_class_context(self, file_path: str, class_name: str, lines_around: int = 10) -> str:
        """获取类的上下文代码"""
        try:
            content = read_file(file_path)
            if not content:
                return ''

            lines = content.split('\n')
            # 查找类定义行
            for i, line in enumerate(lines):
                if f'class {class_name}' in line or f'interface {class_name}' in line:
                    start = max(0, i - lines_around)
                    end = min(len(lines), i + lines_around)
                    return '\n'.join(lines[start:end])

            # 如果没找到类定义，返回文件头部
            return '\n'.join(lines[:lines_around * 2])
        except Exception:
            return ''

    def _get_method_context(self, file_path: str, class_name: str, method_name: str, lines_around: int = 10) -> str:
        """获取方法的上下文代码"""
        try:
            content = read_file(file_path)
            if not content:
                return ''

            lines = content.split('\n')
            # 查找方法定义行
            for i, line in enumerate(lines):
                if f'{method_name}(' in line and ('public' in line or 'private' in line or 'protected' in line):
                    start = max(0, i - lines_around)
                    end = min(len(lines), i + lines_around)
                    return '\n'.join(lines[start:end])

            # 如果没找到方法定义，返回类上下文
            return self._get_class_context(file_path, class_name, lines_around)
        except Exception:
            return ''

    def _fuzzy_search_class(self, class_name: str, max_results: int) -> List[SearchResult]:
        """模糊搜索类名"""
        results = []
        class_name_lower = class_name.lower()

        for nid, attrs in self.graph.graph.nodes(data=True):
            if attrs.get('type') == 'class':
                node_name = attrs.get('name', '').lower()
                full_name = attrs.get('full_name', '').lower()

                # 简单的模糊匹配
                if (class_name_lower in node_name or
                    class_name_lower in full_name or
                    node_name in class_name_lower):

                    file_path = attrs.get('file_path', '')
                    name = attrs.get('name', '')
                    context = self._get_class_context(file_path, name)
                    results.append(SearchResult(
                        file_path=file_path,
                        class_name=name,
                        context_snippet=context,
                        score_hint=50.0  # 模糊匹配给较低分
                    ))

                    if len(results) >= max_results:
                        break

        return results
