"""
Java AST解析适配器 - 使用javalang库解析Java源文件
"""
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import javalang

logger = logging.getLogger(__name__)


class JavaParser:
    """Java AST解析器，提取类、方法、字段、注解、异常处理等信息"""

    def parse_file(self, file_path: str) -> Optional[Dict]:
        """
        解析单个Java文件，返回结构化信息

        Args:
            file_path: Java文件路径

        Returns:
            解析结果字典，失败时返回None
        """
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                source_code = f.read()

            tree = javalang.parse.parse(source_code)
            return self._extract_info(tree, file_path, source_code)
        except Exception as e:
            logger.warning(f"解析文件失败 {file_path}: {e}")
            return None

    def _extract_info(self, tree: javalang.tree.CompilationUnit, file_path: str, source_code: str) -> Dict:
        """从AST中提取结构化信息"""
        info = {
            'file_path': file_path,
            'package': self._extract_package(tree),
            'imports': self._extract_imports(tree),
            'classes': [],
            'exception_handlers': []
        }

        lines = source_code.split('\n')

        for type_decl in tree.types:
            if isinstance(type_decl, (javalang.tree.ClassDeclaration,
                                     javalang.tree.InterfaceDeclaration,
                                     javalang.tree.EnumDeclaration)):
                class_info = self._extract_class_info(type_decl, lines)
                if class_info:
                    info['classes'].append(class_info)
                    # 提取异常处理器
                    info['exception_handlers'].extend(
                        self._extract_exception_handlers(type_decl, class_info['name'])
                    )

        return info

    def _extract_package(self, tree: javalang.tree.CompilationUnit) -> str:
        """提取包名"""
        return tree.package.name if tree.package else ''

    def _extract_imports(self, tree: javalang.tree.CompilationUnit) -> List[str]:
        """提取import语句"""
        imports = []
        for imp in tree.imports:
            imports.append(imp.path)
        return imports

    def _extract_class_info(self, type_decl, lines: List[str]) -> Optional[Dict]:
        """提取类/接口/枚举信息"""
        class_type = 'class'
        if isinstance(type_decl, javalang.tree.InterfaceDeclaration):
            class_type = 'interface'
        elif isinstance(type_decl, javalang.tree.EnumDeclaration):
            class_type = 'enum'

        # 提取注解
        annotations = self._extract_annotations(type_decl)

        # 提取继承关系
        extends = None
        if hasattr(type_decl, 'extends') and type_decl.extends:
            if isinstance(type_decl.extends, list):
                extends = [self._get_type_name(e) for e in type_decl.extends]
            else:
                extends = self._get_type_name(type_decl.extends)

        # 提取实现接口
        implements = []
        if hasattr(type_decl, 'implements') and type_decl.implements:
            implements = [self._get_type_name(i) for i in type_decl.implements]

        # 提取方法
        methods = []
        if hasattr(type_decl, 'methods'):
            for method in type_decl.methods:
                method_info = self._extract_method_info(method, lines)
                if method_info:
                    methods.append(method_info)

        # 提取字段
        fields = []
        if hasattr(type_decl, 'fields'):
            for field in type_decl.fields:
                field_info = self._extract_field_info(field, lines)
                if field_info:
                    fields.extend(field_info)

        # 提取内部类
        inner_classes = []
        if hasattr(type_decl, 'body'):
            for member in type_decl.body:
                if isinstance(member, (javalang.tree.ClassDeclaration,
                                      javalang.tree.InterfaceDeclaration)):
                    inner_class = self._extract_class_info(member, lines)
                    if inner_class:
                        inner_classes.append(inner_class)

        return {
            'name': type_decl.name,
            'type': class_type,
            'annotations': annotations,
            'extends': extends,
            'implements': implements,
            'methods': methods,
            'fields': fields,
            'inner_classes': inner_classes,
            'line': type_decl.position.line if type_decl.position else 0
        }

    def _extract_method_info(self, method, lines: List[str]) -> Optional[Dict]:
        """提取方法信息"""
        annotations = self._extract_annotations(method)

        # 提取返回类型
        return_type = None
        if method.return_type:
            return_type = self._get_type_name(method.return_type)

        # 提取参数
        parameters = []
        if method.parameters:
            for param in method.parameters:
                param_type = self._get_type_name(param.type)
                param_name = param.name
                parameters.append((param_type, param_name))

        # 提取抛出的异常
        throws = []
        if method.throws:
            throws = [t for t in method.throws]

        # 计算方法体行范围
        body_lines = (0, 0)
        if method.position:
            start_line = method.position.line
            # 简单估算方法体结束行
            end_line = min(start_line + 50, len(lines))
            body_lines = (start_line, end_line)

        return {
            'name': method.name,
            'annotations': annotations,
            'return_type': return_type,
            'parameters': parameters,
            'throws': throws,
            'line': method.position.line if method.position else 0,
            'body_lines': body_lines
        }

    def _extract_field_info(self, field, lines: List[str]) -> List[Dict]:
        """提取字段信息"""
        annotations = self._extract_annotations(field)
        field_type = self._get_type_name(field.type)

        fields = []
        for declarator in field.declarators:
            fields.append({
                'name': declarator.name,
                'type': field_type,
                'annotations': annotations,
                'line': field.position.line if field.position else 0
            })

        return fields

    def _extract_annotations(self, node) -> List[str]:
        """提取注解列表"""
        annotations = []
        if hasattr(node, 'annotations') and node.annotations:
            for ann in node.annotations:
                annotations.append(f"@{ann.name}")
        return annotations

    def _extract_exception_handlers(self, type_decl, class_name: str) -> List[Dict]:
        """提取异常处理器（try-catch块）"""
        handlers = []

        # 递归遍历AST查找try语句
        if hasattr(type_decl, 'body'):
            for member in type_decl.body:
                if hasattr(member, 'body'):
                    self._find_try_statements(member.body, class_name, member.name if hasattr(member, 'name') else '', handlers)

        return handlers

    def _find_try_statements(self, statements, class_name: str, method_name: str, handlers: List[Dict]):
        """递归查找try语句"""
        if not statements:
            return

        for stmt in statements:
            if isinstance(stmt, javalang.tree.TryStatement):
                # 提取catch子句
                if stmt.catches:
                    for catch_clause in stmt.catches:
                        for catch_type in catch_clause.types:
                            handlers.append({
                                'class': class_name,
                                'method': method_name,
                                'exception_type': catch_type.name,
                                'handler_line': catch_clause.position.line if catch_clause.position else 0
                            })

            # 递归查找嵌套语句
            if hasattr(stmt, 'block'):
                self._find_try_statements(stmt.block, class_name, method_name, handlers)
            if hasattr(stmt, 'statements'):
                self._find_try_statements(stmt.statements, class_name, method_name, handlers)

    def _get_type_name(self, type_ref) -> str:
        """获取类型名称的字符串表示"""
        if type_ref is None:
            return 'void'

        if isinstance(type_ref, str):
            return type_ref

        if isinstance(type_ref, javalang.tree.ReferenceType):
            name = type_ref.name
            if type_ref.arguments:
                args = [self._get_type_name(arg) for arg in type_ref.arguments]
                name += f"<{', '.join(args)}>"
            if type_ref.dimensions:
                name += '[]' * len(type_ref.dimensions)
            return name

        if isinstance(type_ref, javalang.tree.BasicType):
            name = type_ref.name
            if type_ref.dimensions:
                name += '[]' * len(type_ref.dimensions)
            return name

        return str(type_ref)

    def extract_exception_throws(self, file_path: str) -> List[Tuple[str, str, str]]:
        """
        提取文件中所有throws声明

        Args:
            file_path: Java文件路径

        Returns:
            [(class_name, method_name, exception_type), ...]
        """
        info = self.parse_file(file_path)
        if not info:
            return []

        result = []
        for cls in info['classes']:
            for method in cls['methods']:
                for exc_type in method.get('throws', []):
                    result.append((cls['name'], method['name'], exc_type))

        return result

    def extract_annotations(self, file_path: str) -> List[Tuple[str, str]]:
        """
        提取文件中所有类级注解

        Args:
            file_path: Java文件路径

        Returns:
            [(class_name, annotation), ...]
        """
        info = self.parse_file(file_path)
        if not info:
            return []

        result = []
        for cls in info['classes']:
            for ann in cls['annotations']:
                result.append((cls['name'], ann))

        return result
