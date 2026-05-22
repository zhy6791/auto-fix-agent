"""Unit tests for LLMClient integration."""

import unittest
from unittest.mock import patch, MagicMock

from integrations.llm_client import LLMClient


class TestLLMClient(unittest.TestCase):

    def test_init_with_direct_api_key(self):
        client = LLMClient(api_key='test-key', model='gpt-4o-mini', temperature=0.2)
        self.assertEqual(client.api_key, 'test-key')
        self.assertEqual(client.model, 'gpt-4o-mini')
        self.assertEqual(client.temperature, 0.2)

    def test_init_with_env_var_api_key(self):
        import os
        os.environ['DUMMY_API_KEY'] = 'secret-key'
        client = LLMClient(api_key='$DUMMY_API_KEY')
        self.assertEqual(client.api_key, 'secret-key')

    def test_init_with_missing_env_var(self):
        client = LLMClient(api_key='$NONEXISTENT_VAR')
        self.assertEqual(client.api_key, '')

    def test_init_custom_base_url(self):
        client = LLMClient(api_key='test', base_url='https://custom.api.com/v1')
        self.assertEqual(client.base_url, 'https://custom.api.com/v1')

    def test_init_default_base_url(self):
        client = LLMClient(api_key='test')
        self.assertEqual(client.base_url, 'https://api.openai.com/v1')

    @patch('requests.post')
    def test_generate_patch_success(self, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            'choices': [
                {'message': {'content': '{"files": [{"path": "test.java", "patched_content": "content"}]}'}}
            ]
        }
        mock_post.return_value = mock_response

        client = LLMClient(api_key='test-key')
        result = client.generate_patch('test prompt')

        self.assertIn('files', result)
        self.assertIn('test.java', result)
        mock_post.assert_called_once()

    @patch('requests.post')
    def test_generate_patch_timeout(self, mock_post):
        import requests
        mock_post.side_effect = requests.exceptions.Timeout()

        client = LLMClient(api_key='test-key')
        result = client.generate_patch('test prompt')

        self.assertIn('NO_SAFE_PATCH', result)
        self.assertIn('timeout', result.lower())

    @patch('requests.post')
    def test_generate_patch_connection_error(self, mock_post):
        import requests
        mock_post.side_effect = requests.exceptions.ConnectionError('Connection failed')

        client = LLMClient(api_key='test-key')
        result = client.generate_patch('test prompt')

        self.assertIn('NO_SAFE_PATCH', result)
        self.assertIn('Connection', result)

    @patch('requests.post')
    def test_generate_patch_strips_markdown_fences(self, mock_post):
        fenced = '```diff\n--- a/Foo.java\n+++ b/Foo.java\n@@ -1,1 +1,1 @@\n-old\n+new\n```'
        mock_response = MagicMock()
        mock_response.json.return_value = {
            'choices': [{'message': {'content': fenced}}]
        }
        mock_post.return_value = mock_response

        client = LLMClient(api_key='test-key')
        result = client.generate_patch('test prompt')

        self.assertNotIn('```', result)
        self.assertIn('--- a/Foo.java', result)
        self.assertIn('+new', result)

    def test_generate_patch_no_api_key(self):
        client = LLMClient(api_key='')
        result = client.generate_patch('test prompt')

        self.assertIn('NO_SAFE_PATCH', result)
        self.assertIn('No API key', result)

    @patch('requests.post')
    def test_generate_patch_system_message(self, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            'choices': [{'message': {'content': 'test'}}]
        }
        mock_post.return_value = mock_response

        client = LLMClient(api_key='test-key')
        client.generate_patch('test prompt')

        call_args = mock_post.call_args
        payload = call_args[1]['json']
        messages = payload['messages']
        
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]['role'], 'system')
        self.assertEqual(messages[1]['role'], 'user')


if __name__ == '__main__':
    unittest.main()

