"""
Java仓库知识图谱 - 使用networkx构建代码关系图
"""
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import networkx as nx

from .java_parser import JavaParser
from .inverted_index import InvertedIndex

logger = logging.getLogger(__name__)


class RepoGraph:
    """
    Java仓库知识图谱

    节点类型:
    - class: 类/接口/枚举
    - method: 方法
    - field: 字段

    边类型:
    - calls: 方法调用关系
    - extends: 继承关系
    - implements: 实现关系
    - throws: 抛出异常关系
    - contains: 包含关系（类包含方法/字段）
    - imports: 导入关系
    """

    def __init__(self):
        self.graph = nx.DiGraph()
        self.parser = JavaParser()
        self.index = InvertedIndex()

        # 缓存
        self._file_cache: Dict[str, Dict] = {}
        self._built = False

    def build(self, repo_path: str, max_files: int = 10000, timeout: int = 60):
        """
        构建仓库知识图谱

        Args:
            repo_path: 仓库根目录
            max_files: 最大文件数限制
            timeout: 超时时间（秒）
        """
        import time
        start_time = time.time()

        repo_path = Path(repo_path)
        if not repo_path.exists():
            logger.error(f"仓库路径不存在: {repo_path}")
            return

        # 查找所有Java文件
        java_files = self._find_java_files(repo_path)
        logger.info(f"找到 {len(java_files)} 个Java文件")

        if len(java_files) > max_files:
            logger.warning(f"文件数超过限制({max_files})，只处理前{max_files}个")
            java_files = java_files[:max_files]

        # 解析并构建图
        for i, file_path in enumerate(java_files):
            if time.time() - start_time > timeout:
                logger.warning(f"构建超时({timeout}秒)，已处理 {i}/{len(java_files)} 个文件")
                break

            try:
                file_info = self.parser.parse_file(str(file_path))
                if file_info:
                    self._file_cache[str(file_path)] = file_info
                    self._add_to_graph(file_info)
                    self.index.index_file(file_info)
            except Exception as e:
                logger.warning(f"处理文件失败 {file_path}: {e}")

        self._built = True
        stats = self.get_stats()
        logger.info(f"图构建完成: {stats}")

    def _find_java_files(self, repo_path: Path) -> List[Path]:
        """查找所有Java源文件"""
        java_files = []

        # 优先搜索src/main/java和src/test/java
        for src_dir in ['src/main/java', 'src/test/java']:
            src_path = repo_path / src_dir
            if src_path.exists():
                java_files.extend(src_path.rglob('*.java'))

        # 如果没有找到，搜索整个仓库
        if not java_files:
            java_files = list(repo_path.rglob('*.java'))
            # 排除target目录和依赖目录
            java_files = [f for f in java_files if 'target' not in str(f) and '.m2' not in str(f)]

        return java_files

    def _add_to_graph(self, file_info: Dict):
        """将文件信息添加到图中"""
        file_path = file_info['file_path']
        package = file_info.get('package', '')

        for cls in file_info.get('classes', []):
            class_name = cls['name']
            full_class_name = f"{package}.{class_name}" if package else class_name

            # 添加类节点
            class_node_id = f"class:{full_class_name}"
            self.graph.add_node(class_node_id, **{
                'type': 'class',
                'name': class_name,
                'full_name': full_class_name,
                'file_path': file_path,
                'annotations': cls.get('annotations', []),
                'class_type': cls.get('type', 'class'),
                'line': cls.get('line', 0)
            })

            # 添加继承边
            extends = cls.get('extends')
            if extends:
                if isinstance(extends, list):
                    for ext in extends:
                        ext_node_id = f"class:{ext}"
                        self.graph.add_edge(class_node_id, ext_node_id, type='extends')
                else:
                    ext_node_id = f"class:{extends}"
                    self.graph.add_edge(class_node_id, ext_node_id, type='extends')

            # 添加实现边
            for impl in cls.get('implements', []):
                impl_node_id = f"class:{impl}"
                self.graph.add_edge(class_node_id, impl_node_id, type='implements')

            # 添加方法节点
            for method in cls.get('methods', []):
                method_name = method['name']
                method_node_id = f"method:{full_class_name}.{method_name}"

                self.graph.add_node(method_node_id, **{
                    'type': 'method',
                    'name': method_name,
                    'class_name': full_class_name,
                    'file_path': file_path,
                    'annotations': method.get('annotations', []),
                    'return_type': method.get('return_type'),
                    'parameters': method.get('parameters', []),
                    'throws': method.get('throws', []),
                    'line': method.get('line', 0)
                })

                # 添加包含边（类包含方法）
                self.graph.add_edge(class_node_id, method_node_id, type='contains')

                # 添加throws边
                for exc_type in method.get('throws', []):
                    exc_node_id = f"exception:{exc_type}"
                    self.graph.add_node(exc_node_id, type='exception', name=exc_type)
                    self.graph.add_edge(method_node_id, exc_node_id, type='throws')

            # 添加字段节点
            for field in cls.get('fields', []):
                field_name = field['name']
                field_node_id = f"field:{full_class_name}.{field_name}"

                self.graph.add_node(field_node_id, **{
                    'type': 'field',
                    'name': field_name,
                    'class_name': full_class_name,
                    'file_path': file_path,
                    'field_type': field.get('type'),
                    'annotations': field.get('annotations', []),
                    'line': field.get('line', 0)
                })

                # 添加包含边（类包含字段）
                self.graph.add_edge(class_node_id, field_node_id, type='contains')

        # 添加import关系（近似调用关系）
        for imp in file_info.get('imports', []):
            if not imp.startswith('java.') and not imp.startswith('javax.'):
                imp_node_id = f"class:{imp}"
                if imp_node_id in self.graph:
                    for cls in file_info.get('classes', []):
                        class_name = cls['name']
                        full_class_name = f"{package}.{class_name}" if package else class_name
                        class_node_id = f"class:{full_class_name}"
                        self.graph.add_edge(class_node_id, imp_node_id, type='imports')

    def query_class(self, class_name: str) -> Optional[Dict]:
        """
        查询类信息

        Args:
            class_name: 类名（简单类名或全限定名）

        Returns:
            类节点属性字典，未找到返回None
        """
        # 尝试全限定名
        node_id = f"class:{class_name}"
        if node_id in self.graph:
            return dict(self.graph.nodes[node_id])

        # 尝试简单类名匹配
        for nid, attrs in self.graph.nodes(data=True):
            if attrs.get('type') == 'class' and attrs.get('name') == class_name:
                return dict(attrs)

        return None

    def query_method(self, class_name: str, method_name: str) -> Optional[Dict]:
        """
        查询方法信息

        Args:
            class_name: 类名
            method_name: 方法名

        Returns:
            方法节点属性字典，未找到返回None
        """
        # 尝试全限定名
        node_id = f"method:{class_name}.{method_name}"
        if node_id in self.graph:
            return dict(self.graph.nodes[node_id])

        # 尝试简单类名匹配
        for nid, attrs in self.graph.nodes(data=True):
            if (attrs.get('type') == 'method' and
                attrs.get('name') == method_name and
                attrs.get('class_name', '').endswith(f'.{class_name}')):
                return dict(attrs)

        return None

    def get_callers(self, class_name: str, method_name: str) -> List[Dict]:
        """
        获取调用指定方法的方法列表

        Args:
            class_name: 类名
            method_name: 方法名

        Returns:
            调用者方法列表
        """
        method_node_id = f"method:{class_name}.{method_name}"
        if method_node_id not in self.graph:
            return []

        callers = []
        for pred in self.graph.predecessors(method_node_id):
            edge_data = self.graph.get_edge_data(pred, method_node_id)
            if edge_data and edge_data.get('type') == 'calls':
                callers.append(dict(self.graph.nodes[pred]))

        return callers

    def get_callees(self, class_name: str, method_name: str) -> List[Dict]:
        """
        获取指定方法调用的方法列表

        Args:
            class_name: 类名
            method_name: 方法名

        Returns:
            被调用者方法列表
        """
        method_node_id = f"method:{class_name}.{method_name}"
        if method_node_id not in self.graph:
            return []

        callees = []
        for succ in self.graph.successors(method_node_id):
            edge_data = self.graph.get_edge_data(method_node_id, succ)
            if edge_data and edge_data.get('type') == 'calls':
                callees.append(dict(self.graph.nodes[succ]))

        return callees

    def get_exception_flow(self, exception_type: str) -> List[Dict]:
        """
        获取与指定异常类型相关的方法

        Args:
            exception_type: 异常类型

        Returns:
            相关方法列表
        """
        exc_node_id = f"exception:{exception_type}"
        if exc_node_id not in self.graph:
            # 尝试简单类名匹配
            for nid, attrs in self.graph.nodes(data=True):
                if attrs.get('type') == 'exception' and attrs.get('name') == exception_type:
                    exc_node_id = nid
                    break
            else:
                return []

        related_methods = []
        for pred in self.graph.predecessors(exc_node_id):
            edge_data = self.graph.get_edge_data(pred, exc_node_id)
            if edge_data and edge_data.get('type') == 'throws':
                related_methods.append(dict(self.graph.nodes[pred]))

        return related_methods

    def get_class_hierarchy(self, class_name: str) -> Dict:
        """
        获取类的继承层次

        Args:
            class_name: 类名

        Returns:
            包含父类和子类的字典
        """
        class_node_id = f"class:{class_name}"
        if class_node_id not in self.graph:
            return {'parents': [], 'children': []}

        parents = []
        children = []

        # 获取父类
        for succ in self.graph.successors(class_node_id):
            edge_data = self.graph.get_edge_data(class_node_id, succ)
            if edge_data and edge_data.get('type') in ('extends', 'implements'):
                parents.append(dict(self.graph.nodes[succ]))

        # 获取子类
        for pred in self.graph.predecessors(class_node_id):
            edge_data = self.graph.get_edge_data(pred, class_node_id)
            if edge_data and edge_data.get('type') in ('extends', 'implements'):
                children.append(dict(self.graph.nodes[pred]))

        return {'parents': parents, 'children': children}

    def find_related_classes(self, class_name: str, max_depth: int = 2) -> List[Dict]:
        """
        查找相关类（通过继承、实现、调用关系）

        Args:
            class_name: 类名
            max_depth: 最大搜索深度

        Returns:
            相关类列表
        """
        class_node_id = f"class:{class_name}"
        if class_node_id not in self.graph:
            return []

        related = set()
        queue = [(class_node_id, 0)]
        visited = {class_node_id}

        while queue:
            node_id, depth = queue.pop(0)
            if depth >= max_depth:
                continue

            # 遍历邻居
            for neighbor in self.graph.neighbors(node_id):
                if neighbor not in visited:
                    visited.add(neighbor)
                    neighbor_attrs = self.graph.nodes[neighbor]
                    if neighbor_attrs.get('type') == 'class':
                        related.add(neighbor)
                    queue.append((neighbor, depth + 1))

            # 遍历前驱
            for predecessor in self.graph.predecessors(node_id):
                if predecessor not in visited:
                    visited.add(predecessor)
                    pred_attrs = self.graph.nodes[predecessor]
                    if pred_attrs.get('type') == 'class':
                        related.add(predecessor)
                    queue.append((predecessor, depth + 1))

        return [dict(self.graph.nodes[nid]) for nid in related]

    def get_stats(self) -> Dict:
        """获取图统计信息"""
        node_types = {}
        edge_types = {}

        for _, attrs in self.graph.nodes(data=True):
            node_type = attrs.get('type', 'unknown')
            node_types[node_type] = node_types.get(node_type, 0) + 1

        for _, _, attrs in self.graph.edges(data=True):
            edge_type = attrs.get('type', 'unknown')
            edge_types[edge_type] = edge_types.get(edge_type, 0) + 1

        return {
            'total_nodes': self.graph.number_of_nodes(),
            'total_edges': self.graph.number_of_edges(),
            'node_types': node_types,
            'edge_types': edge_types,
            'files_processed': len(self._file_cache),
            'index_stats': self.index.get_stats()
        }

    def is_built(self) -> bool:
        """检查图是否已构建"""
        return self._built

    def clear(self):
        """清空图"""
        self.graph.clear()
        self.index.clear()
        self._file_cache.clear()
        self._built = False
