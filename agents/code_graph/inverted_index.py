"""
倒排索引 - 用于快速代码检索
"""
import logging
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class InvertedIndex:
    """
    倒排索引，支持多种维度的快速代码检索

    索引维度:
    - exception_type: 异常类型 -> [抛出/捕获该异常的class.method对]
    - class_name: 类名 -> [文件路径]
    - method_name: 方法名 -> [(类名, 文件路径, 行号)]
    - annotation: 注解 -> [被注解的类名]
    """

    def __init__(self):
        # 异常类型索引: exception_type -> [(class_name, method_name, file_path)]
        self.exception_index: Dict[str, List[Tuple[str, str, str]]] = defaultdict(list)

        # 类名索引: class_name -> [file_path]
        self.class_index: Dict[str, List[str]] = defaultdict(list)

        # 方法名索引: method_name -> [(class_name, file_path, line_no)]
        self.method_index: Dict[str, List[Tuple[str, str, int]]] = defaultdict(list)

        # 注解索引: annotation -> [(class_name, file_path)]
        self.annotation_index: Dict[str, List[Tuple[str, str]]] = defaultdict(list)

        # 类-方法关系索引: (class_name, method_name) -> file_path
        self.class_method_index: Dict[Tuple[str, str], str] = {}

    def index_file(self, file_info: Dict):
        """
        索引单个文件的解析结果

        Args:
            file_info: JavaParser.parse_file()的返回结果
        """
        if not file_info:
            return

        file_path = file_info['file_path']
        package = file_info.get('package', '')

        # 索引类
        for cls in file_info.get('classes', []):
            class_name = cls['name']
            full_class_name = f"{package}.{class_name}" if package else class_name

            # 类名索引
            self.class_index[class_name].append(file_path)
            self.class_index[full_class_name].append(file_path)

            # 注解索引
            for ann in cls.get('annotations', []):
                self.annotation_index[ann].append((class_name, file_path))

            # 索引方法
            for method in cls.get('methods', []):
                method_name = method['name']
                line_no = method.get('line', 0)

                # 方法名索引
                self.method_index[method_name].append((class_name, file_path, line_no))

                # 类-方法关系索引
                self.class_method_index[(class_name, method_name)] = file_path

                # 索引throws声明
                for exc_type in method.get('throws', []):
                    self.exception_index[exc_type].append((class_name, method_name, file_path))

        # 索引异常处理器
        for handler in file_info.get('exception_handlers', []):
            exc_type = handler.get('exception_type', '')
            class_name = handler.get('class', '')
            method_name = handler.get('method', '')
            if exc_type and class_name:
                self.exception_index[exc_type].append((class_name, method_name, file_path))

    def search_by_exception(self, exception_type: str) -> List[Tuple[str, str, str]]:
        """
        按异常类型搜索

        Args:
            exception_type: 异常类型名称（可以是简单类名或全限定名）

        Returns:
            [(class_name, method_name, file_path), ...]
        """
        # 精确匹配
        results = self.exception_index.get(exception_type, [])

        # 如果没有精确匹配，尝试简单类名匹配
        if not results and '.' in exception_type:
            simple_name = exception_type.split('.')[-1]
            results = self.exception_index.get(simple_name, [])

        # 去重
        return list(set(results))

    def search_by_class(self, class_name: str) -> List[str]:
        """
        按类名搜索文件路径

        Args:
            class_name: 类名（可以是简单类名或全限定名）

        Returns:
            [file_path, ...]
        """
        # 精确匹配
        results = self.class_index.get(class_name, [])

        # 如果没有精确匹配，尝试简单类名匹配
        if not results and '.' in class_name:
            simple_name = class_name.split('.')[-1]
            results = self.class_index.get(simple_name, [])

        # 去重
        return list(set(results))

    def search_by_method(self, method_name: str) -> List[Tuple[str, str, int]]:
        """
        按方法名搜索

        Args:
            method_name: 方法名

        Returns:
            [(class_name, file_path, line_no), ...]
        """
        return self.method_index.get(method_name, [])

    def search_by_annotation(self, annotation: str) -> List[Tuple[str, str]]:
        """
        按注解搜索

        Args:
            annotation: 注解名称（如@Service, @Controller）

        Returns:
            [(class_name, file_path), ...]
        """
        # 确保注解以@开头
        if not annotation.startswith('@'):
            annotation = f'@{annotation}'

        return self.annotation_index.get(annotation, [])

    def get_file_path(self, class_name: str, method_name: Optional[str] = None) -> Optional[str]:
        """
        获取类或方法所在的文件路径

        Args:
            class_name: 类名
            method_name: 方法名（可选）

        Returns:
            文件路径，未找到返回None
        """
        if method_name:
            return self.class_method_index.get((class_name, method_name))

        # 只按类名查找
        files = self.class_index.get(class_name, [])
        return files[0] if files else None

    def get_stats(self) -> Dict:
        """获取索引统计信息"""
        return {
            'exception_types': len(self.exception_index),
            'classes': len(self.class_index),
            'methods': len(self.method_index),
            'annotations': len(self.annotation_index),
            'class_method_pairs': len(self.class_method_index)
        }

    def merge(self, other: 'InvertedIndex'):
        """
        合并另一个索引

        Args:
            other: 另一个InvertedIndex实例
        """
        for k, v in other.exception_index.items():
            self.exception_index[k].extend(v)
        for k, v in other.class_index.items():
            self.class_index[k].extend(v)
        for k, v in other.method_index.items():
            self.method_index[k].extend(v)
        for k, v in other.annotation_index.items():
            self.annotation_index[k].extend(v)
        self.class_method_index.update(other.class_method_index)

    def clear(self):
        """清空所有索引"""
        self.exception_index.clear()
        self.class_index.clear()
        self.method_index.clear()
        self.annotation_index.clear()
        self.class_method_index.clear()
