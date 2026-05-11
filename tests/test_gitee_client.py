"""Unit tests for GiteeClient integration."""

import unittest
from unittest.mock import patch, MagicMock

from integrations.gitee_client import GiteeClient


class TestGiteeClient(unittest.TestCase):

    def test_init_defaults(self):
        client = GiteeClient(access_token='test-token')
        self.assertEqual(client.access_token, 'test-token')
        self.assertEqual(client.base_url, 'https://gitee.com/api/v5')
        self.assertEqual(client.timeout, 30)

    def test_init_custom_base_url(self):
        client = GiteeClient(access_token='test-token', base_url='https://gitee.example.com/api/v5')
        self.assertEqual(client.base_url, 'https://gitee.example.com/api/v5')

    @patch('requests.post')
    def test_create_pr_success(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            'html_url': 'https://gitee.com/owner/repo/pulls/42',
            'number': 42,
        }
        mock_post.return_value = mock_response

        client = GiteeClient(access_token='test-token')
        result = client.create_pull_request(
            owner='owner', repo='repo', title='fix: test',
            head='fix/auto-test', base='main', body='test body'
        )

        self.assertTrue(result['success'])
        self.assertEqual(result['url'], 'https://gitee.com/owner/repo/pulls/42')
        self.assertEqual(result['number'], 42)
        self.assertIsNone(result['error'])

        mock_post.assert_called_once()
        call_args = mock_post.call_args
        self.assertEqual(call_args[1]['params'], {'access_token': 'test-token'})

    @patch('requests.post')
    def test_create_pr_http_error(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 422
        mock_response.json.return_value = {'message': 'Validation failed'}
        mock_post.return_value = mock_response

        client = GiteeClient(access_token='test-token')
        result = client.create_pull_request(
            owner='owner', repo='repo', title='test',
            head='fix/test', base='main'
        )

        self.assertFalse(result['success'])
        self.assertIn('422', result['error'])
        self.assertIn('Validation failed', result['error'])

    @patch('requests.post')
    def test_create_pr_timeout(self, mock_post):
        import requests
        mock_post.side_effect = requests.exceptions.Timeout()

        client = GiteeClient(access_token='test-token')
        result = client.create_pull_request(
            owner='owner', repo='repo', title='test',
            head='fix/test', base='main'
        )

        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'Request timeout')

    @patch('requests.post')
    def test_create_pr_connection_error(self, mock_post):
        import requests
        mock_post.side_effect = requests.exceptions.ConnectionError('Connection refused')

        client = GiteeClient(access_token='test-token')
        result = client.create_pull_request(
            owner='owner', repo='repo', title='test',
            head='fix/test', base='main'
        )

        self.assertFalse(result['success'])
        self.assertIn('Connection error', result['error'])

    @patch('requests.get')
    def test_get_repo_info_success(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'default_branch': 'master'}
        mock_get.return_value = mock_response

        client = GiteeClient(access_token='test-token')
        result = client.get_repo_info('owner', 'repo')

        self.assertTrue(result['success'])
        self.assertEqual(result['default_branch'], 'master')

    @patch('requests.get')
    def test_get_repo_info_failure(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        client = GiteeClient(access_token='test-token')
        result = client.get_repo_info('owner', 'nonexistent')

        self.assertFalse(result['success'])
        self.assertIn('404', result['error'])


if __name__ == '__main__':
    unittest.main()
