"""LLM client for OpenAI-compatible APIs (with custom base_url support)."""

import json
import os
import re
import time
import logging
import requests

logger = logging.getLogger(__name__)

# HTTP status codes that are safe to retry
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def _strip_markdown_fences(text):
    """Strip markdown code fences from LLM response."""
    if not text:
        return text
    lines = text.splitlines()
    if lines and lines[0].strip().startswith('```'):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith('```'):
        lines = lines[:-1]
    lines = [ln for ln in lines if ln.strip() != '```']
    return '\n'.join(lines)


class LLMClient:
    def __init__(self, api_key, model: str = 'gpt-4o-mini', temperature: float = 0.2, base_url: str = None, timeout: int = 300, max_retries: int = 3):
        """Initialize LLM client.

        Args:
            api_key: API key string or environment variable name (if starts with $).
            model: Model name (default: gpt-4o-mini).
            temperature: Temperature for generation (default: 0.2).
            base_url: Optional base URL for API endpoint (default: https://api.openai.com/v1).
            timeout: Request timeout in seconds (default: 300).
            max_retries: Max retry attempts for transient errors (default: 3).
        """
        # Handle api_key: if starts with $, read from environment; else use directly
        if isinstance(api_key, str) and api_key.startswith('$'):
            self.api_key = os.environ.get(api_key[1:], '')
        else:
            self.api_key = api_key

        self.model = model
        self.temperature = temperature
        self.base_url = base_url or "https://api.openai.com/v1"
        self.timeout = timeout
        self.max_retries = max_retries

    def generate_patch(self, prompt: str, max_tokens: int = 1024) -> str:
        """Call OpenAI-compatible API to generate a patch.

        Returns the raw response (typically unified-diff or JSON).
        Retries on transient errors (429/500/502/503/504) with exponential backoff.
        Returns NO_SAFE_PATCH on permanent failure.
        """
        if not self.api_key:
            logger.error('No API key configured for LLM')
            return 'NO_SAFE_PATCH: No API key configured'

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": max_tokens,
            "messages": [
                {
                    "role": "system",
                    "content": "你是一个专业的 Java Web 服务自动修复助手。请生成高质量的最小化补丁。"
                },
                {"role": "user", "content": prompt}
            ]
        }

        url = f"{self.base_url}/chat/completions"
        last_error = None

        for attempt in range(self.max_retries + 1):
            try:
                if attempt > 0:
                    delay = min(2 ** attempt, 30)
                    logger.info(f"LLM retry {attempt}/{self.max_retries}, waiting {delay}s...")
                    time.sleep(delay)

                logger.debug(f"Calling LLM at {url} with model={self.model}")
                resp = requests.post(url, json=payload, headers=headers, timeout=self.timeout)

                if resp.status_code in _RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                    logger.warning(f"LLM returned retryable status {resp.status_code}, will retry")
                    last_error = f"HTTP {resp.status_code}"
                    continue

                resp.raise_for_status()
                data = resp.json()
                result = data['choices'][0]['message']['content']
                result = _strip_markdown_fences(result)
                logger.debug(f"Raw LLM response length: {len(result)}, content preview: {result[:200]}...")
                if not result or not result.strip():
                    logger.warning('LLM returned empty or whitespace-only response')
                    return 'NO_SAFE_PATCH: LLM returned empty response'
                logger.info(f"LLM response: {len(result)} characters")
                return result

            except requests.exceptions.Timeout:
                last_error = 'timeout'
                if attempt < self.max_retries:
                    logger.warning(f"LLM request timeout, will retry ({attempt + 1}/{self.max_retries})")
                    continue
                logger.error('LLM request timeout (all retries exhausted)')
                return 'NO_SAFE_PATCH: LLM request timeout'
            except requests.exceptions.ConnectionError as e:
                last_error = str(e)
                if attempt < self.max_retries:
                    logger.warning(f"LLM connection error, will retry ({attempt + 1}/{self.max_retries}): {e}")
                    continue
                logger.error(f'LLM connection error: {e}')
                return f'NO_SAFE_PATCH: Connection error - {e}'
            except requests.exceptions.HTTPError as e:
                response_text = ''
                if e.response is not None:
                    try:
                        response_text = e.response.text.strip()
                    except Exception:
                        response_text = ''

                if response_text:
                    logger.error(f'LLM HTTP error: {e}; response body: {response_text}')
                    return f'NO_SAFE_PATCH: HTTP error - {e.response.status_code}: {response_text}'

                logger.error(f'LLM HTTP error: {e}')
                return f'NO_SAFE_PATCH: HTTP error - {e.response.status_code}'
            except Exception as e:
                logger.exception('LLM call failed unexpectedly')
                return f'NO_SAFE_PATCH: LLM call failed - {type(e).__name__}: {e}'

        return f'NO_SAFE_PATCH: LLM failed after {self.max_retries} retries (last: {last_error})'

    def chat(self, messages, tools=None, max_tokens=4096, temperature=None):
        """General-purpose chat completion with optional tool/function calling.

        Args:
            messages: OpenAI-format message list.
            tools: Optional list of tool schemas (OpenAI function calling format).
            max_tokens: Max response tokens.
            temperature: Override temperature (default: use self.temperature).

        Returns:
            dict with keys:
            - "content": str (text response, may be empty if tool call)
            - "tool_calls": list[dict] or None (each has "name" and "arguments")
            - "finish_reason": str
        """
        if not self.api_key:
            return {'content': 'NO_SAFE_PATCH: No API key configured', 'tool_calls': None, 'finish_reason': 'error'}

        headers = {
            'Authorization': 'Bearer %s' % self.api_key,
            'Content-Type': 'application/json',
        }

        payload = {
            'model': self.model,
            'temperature': temperature if temperature is not None else self.temperature,
            'max_tokens': max_tokens,
            'messages': messages,
        }

        if tools:
            payload['tools'] = tools

        url = '%s/chat/completions' % self.base_url
        last_error = None

        for attempt in range(self.max_retries + 1):
            try:
                if attempt > 0:
                    delay = min(2 ** attempt, 30)
                    logger.info('LLM chat retry %d/%d, waiting %ds...', attempt, self.max_retries, delay)
                    time.sleep(delay)

                resp = requests.post(url, json=payload, headers=headers, timeout=self.timeout)

                if resp.status_code in _RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                    logger.warning('LLM chat returned retryable status %d', resp.status_code)
                    last_error = 'HTTP %d' % resp.status_code
                    continue

                resp.raise_for_status()
                data = resp.json()
                choice = data['choices'][0]
                message = choice.get('message', {})
                content = message.get('content', '') or ''
                finish_reason = choice.get('finish_reason', 'stop')

                # Try to extract tool_calls from the response
                tool_calls = None
                raw_tool_calls = message.get('tool_calls')
                if raw_tool_calls:
                    tool_calls = []
                    for tc in raw_tool_calls:
                        fn = tc.get('function', {})
                        name = fn.get('name', '')
                        args_str = fn.get('arguments', '{}')
                        try:
                            args = json.loads(args_str) if isinstance(args_str, str) else args_str
                        except (json.JSONDecodeError, TypeError):
                            args = {}
                        tool_calls.append({'name': name, 'arguments': args, 'id': tc.get('id')})

                # Fallback: parse tool call from text content
                if not tool_calls and content:
                    parsed = _parse_text_tool_call(content)
                    if parsed:
                        tool_calls = [parsed]
                        finish_reason = 'tool_calls'

                return {
                    'content': content,
                    'tool_calls': tool_calls,
                    'finish_reason': finish_reason,
                }

            except requests.exceptions.Timeout:
                last_error = 'timeout'
                if attempt < self.max_retries:
                    logger.warning('LLM chat timeout, will retry (%d/%d)', attempt + 1, self.max_retries)
                    continue
            except requests.exceptions.ConnectionError as e:
                last_error = str(e)
                if attempt < self.max_retries:
                    logger.warning('LLM chat connection error, will retry: %s', e)
                    continue
            except requests.exceptions.HTTPError as e:
                logger.error('LLM chat HTTP error: %s', e)
                return {'content': 'NO_SAFE_PATCH: HTTP error - %s' % e, 'tool_calls': None, 'finish_reason': 'error'}
            except Exception as e:
                logger.exception('LLM chat call failed')
                return {'content': 'NO_SAFE_PATCH: %s' % e, 'tool_calls': None, 'finish_reason': 'error'}

        return {
            'content': 'NO_SAFE_PATCH: LLM failed after %d retries (last: %s)' % (self.max_retries, last_error),
            'tool_calls': None,
            'finish_reason': 'error',
        }


def _parse_text_tool_call(text):
    """Parse a Thought/Action format from LLM text response.

    Looks for:
        Thought: <reasoning>
        Action: <tool_name>(<json_arguments>)

    Returns dict with "name" and "arguments", or None if not found.
    """
    if not text:
        return None

    # Try JSON block first: {"tool": "name", "arguments": {...}}
    json_match = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
    if json_match:
        try:
            obj = json.loads(json_match.group(1))
            if 'tool' in obj:
                return {'name': obj['tool'], 'arguments': obj.get('arguments', {})}
        except (json.JSONDecodeError, KeyError):
            pass

    # Try bare JSON with "tool" key
    json_match = re.search(r'\{[^{}]*"tool"\s*:\s*"(\w+)"[^{}]*\}', text)
    if json_match:
        try:
            start = text.index('{')
            end = text.rindex('}') + 1
            obj = json.loads(text[start:end])
            if 'tool' in obj:
                return {'name': obj['tool'], 'arguments': obj.get('arguments', {})}
        except (json.JSONDecodeError, ValueError):
            pass

    # Try Action: tool_name({json})
    action_match = re.search(r'Action:\s*(\w+)\((.*?)\)\s*$', text, re.DOTALL | re.MULTILINE)
    if action_match:
        tool_name = action_match.group(1)
        args_str = action_match.group(2).strip()
        try:
            args = json.loads(args_str) if args_str else {}
        except json.JSONDecodeError:
            args = {}
        return {'name': tool_name, 'arguments': args}

    return None



