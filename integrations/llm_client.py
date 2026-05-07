"""LLM client for OpenAI-compatible APIs (with custom base_url support)."""

import os
import logging
import requests

logger = logging.getLogger(__name__)


class LLMClient:
    def __init__(self, api_key, model: str = 'gpt-4o-mini', temperature: float = 0.2, base_url: str = None, timeout: int = 300):
        """Initialize LLM client.

        Args:
            api_key: API key string or environment variable name (if starts with $).
            model: Model name (default: gpt-4o-mini).
            temperature: Temperature for generation (default: 0.2).
            base_url: Optional base URL for API endpoint (default: https://api.openai.com/v1).
            timeout: Request timeout in seconds (default: 300).
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

    def generate_patch(self, prompt: str, max_tokens: int = 1024) -> str:
        """Call OpenAI-compatible API to generate a patch.
        
        Returns the raw response (typically unified-diff or JSON).
        Raises or returns NO_SAFE_PATCH on failure.
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
        try:
            logger.debug(f"Calling LLM at {url} with model={self.model}")
            resp = requests.post(url, json=payload, headers=headers, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            result = data['choices'][0]['message']['content']
            logger.info(f"LLM response: {len(result)} characters")
            return result
        except requests.exceptions.Timeout:
            logger.error('LLM request timeout')
            return 'NO_SAFE_PATCH: LLM request timeout'
        except requests.exceptions.ConnectionError as e:
            logger.error(f'LLM connection error: {e}')
            return f'NO_SAFE_PATCH: Connection error - {e}'
        except requests.exceptions.HTTPError as e:
            logger.error(f'LLM HTTP error: {e}')
            return f'NO_SAFE_PATCH: HTTP error - {e.response.status_code}'
        except Exception as e:
            logger.exception('LLM call failed unexpectedly')
            return f'NO_SAFE_PATCH: LLM call failed - {type(e).__name__}: {e}'



