"""
LLM相关性评分器 - 使用LLM对代码候选位置进行相关性评分
"""
import json
import logging
from typing import Dict, List

logger = logging.getLogger(__name__)


class CodeScorer:
    """
    LLM相关性评分器

    接收候选位置列表+异常上下文，调用LLM对每个候选评分0-100
    批量评分（单次LLM调用处理所有候选），最小化API开销
    """

    def __init__(self, llm_client):
        """
        初始化评分器

        Args:
            llm_client: LLM客户端实例（需要有chat方法）
        """
        self.llm_client = llm_client

    def score_candidates(self, candidates: List[Dict], exception_context: str,
                        max_candidates: int = 10) -> List[Dict]:
        """
        对候选位置进行相关性评分

        Args:
            candidates: 候选位置列表，每个候选包含file_path, class_name, method_name, context_snippet
            exception_context: 异常上下文信息（异常类型、消息、堆栈等）
            max_candidates: 最大评分候选数

        Returns:
            按分数排序的候选列表，每个候选添加score字段
        """
        if not candidates:
            return []

        # 限制候选数量
        candidates_to_score = candidates[:max_candidates]

        try:
            # 构建批量评分prompt
            prompt = self._build_scoring_prompt(candidates_to_score, exception_context)

            # 调用LLM进行评分
            response = self.llm_client.chat(prompt)

            # 解析评分结果
            scores = self._parse_scores(response, len(candidates_to_score))

            # 合并分数到候选列表
            scored_candidates = []
            for i, candidate in enumerate(candidates_to_score):
                candidate_copy = candidate.copy()
                if i < len(scores):
                    candidate_copy['score'] = scores[i]
                else:
                    candidate_copy['score'] = candidate.get('score_hint', 50.0)
                scored_candidates.append(candidate_copy)

            # 按分数降序排序
            scored_candidates.sort(key=lambda x: x.get('score', 0), reverse=True)

            return scored_candidates

        except Exception as e:
            logger.warning(f"LLM评分失败，使用默认分数: {e}")
            # 评分失败时使用默认分数
            for candidate in candidates_to_score:
                candidate['score'] = candidate.get('score_hint', 50.0)
            return candidates_to_score

    def _build_scoring_prompt(self, candidates: List[Dict], exception_context: str) -> str:
        """构建评分prompt"""
        prompt = """你是一个Java代码分析专家。请分析以下异常信息和候选代码位置，对每个候选位置与异常的相关性进行评分。

## 异常信息
{exception_context}

## 候选代码位置
请对以下每个候选位置评分（0-100分），评分标准：
- 100分：极有可能是bug所在位置
- 75-99分：很可能是bug相关位置
- 50-74分：有一定相关性
- 25-49分：相关性较低
- 0-24分：基本不相关

请以JSON数组格式返回评分，例如：[85, 72, 45, 30, 15]

## 候选列表
"""
        for i, candidate in enumerate(candidates):
            file_path = candidate.get('file_path', '')
            class_name = candidate.get('class_name', '')
            method_name = candidate.get('method_name', '')
            context = candidate.get('context_snippet', '')

            prompt += f"""
### 候选 {i+1}
- 文件: {file_path}
- 类: {class_name}
- 方法: {method_name}
- 代码片段:
```
{context[:500]}  # 限制代码片段长度
```
"""

        prompt += "\n请直接返回评分JSON数组，不要添加其他内容。"
        return prompt

    def _parse_scores(self, response: str, expected_count: int) -> List[float]:
        """解析LLM返回的评分"""
        try:
            # 尝试直接解析JSON数组
            # 先清理响应，移除可能的markdown代码块标记
            clean_response = response.strip()
            if clean_response.startswith('```'):
                # 移除markdown代码块
                lines = clean_response.split('\n')
                clean_response = '\n'.join(lines[1:-1])

            scores = json.loads(clean_response)

            if isinstance(scores, list):
                # 确保所有元素都是数字
                result = []
                for score in scores:
                    if isinstance(score, (int, float)):
                        result.append(float(score))
                    else:
                        result.append(50.0)  # 默认分数

                # 补齐或截断到预期数量
                while len(result) < expected_count:
                    result.append(50.0)

                return result[:expected_count]

        except json.JSONDecodeError as e:
            logger.warning(f"解析评分JSON失败: {e}")

        # 如果JSON解析失败，尝试提取数字
        import re
        numbers = re.findall(r'\d+', response)
        if numbers:
            result = [float(n) for n in numbers[:expected_count]]
            while len(result) < expected_count:
                result.append(50.0)
            return result

        # 全部使用默认分数
        return [50.0] * expected_count

    def score_single(self, candidate: Dict, exception_context: str) -> float:
        """
        对单个候选进行评分

        Args:
            candidate: 候选位置信息
            exception_context: 异常上下文

        Returns:
            评分（0-100）
        """
        results = self.score_candidates([candidate], exception_context, max_candidates=1)
        if results:
            return results[0].get('score', 50.0)
        return 50.0

    def batch_score(self, candidate_groups: List[List[Dict]], exception_context: str) -> List[List[Dict]]:
        """
        批量评分多组候选

        Args:
            candidate_groups: 候选组列表
            exception_context: 异常上下文

        Returns:
            每组的评分结果
        """
        results = []
        for group in candidate_groups:
            scored = self.score_candidates(group, exception_context)
            results.append(scored)
        return results
