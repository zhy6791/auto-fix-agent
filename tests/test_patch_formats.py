"""Unit tests for patch format parsing and application."""

import os
import json
import shutil
import tempfile
import subprocess
import unittest

from tools import git_manager


class TestPatchFormats(unittest.TestCase):

    def setUp(self):
        self.workdir = tempfile.mkdtemp()
        self.repo_path = os.path.join(self.workdir, 'test-repo')
        os.makedirs(self.repo_path)

        subprocess.check_call(['git', 'init'], cwd=self.repo_path)
        subprocess.check_call(['git', 'config', 'user.name', 'Test'], cwd=self.repo_path)
        subprocess.check_call(['git', 'config', 'user.email', 'test@test.com'], cwd=self.repo_path)

        # Create initial commit
        self.test_file = os.path.join(self.repo_path, 'src', 'TestFile.txt')
        os.makedirs(os.path.dirname(self.test_file))
        with open(self.test_file, 'w') as f:
            f.write('Line 1\nLine 2\nLine 3\n')
        subprocess.check_call(['git', 'add', '.'], cwd=self.repo_path)
        subprocess.check_call(['git', 'commit', '-m', 'Initial'], cwd=self.repo_path)

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_apply_patch_json_format(self):
        patch_obj = {
            'files': [
                {
                    'path': 'src/TestFile.txt',
                    'patched_content': 'Line 1 Modified\nLine 2\nLine 3\n'
                }
            ]
        }
        patch_text = json.dumps(patch_obj)
        result = git_manager.apply_patch(self.repo_path, patch_text)

        self.assertTrue(result['applied'])
        self.assertIn('src/TestFile.txt', result['files'])
        with open(self.test_file) as f:
            content = f.read()
        self.assertIn('Line 1 Modified', content)

    def test_apply_patch_unified_diff_format(self):
        patch_text = '''--- a/src/TestFile.txt
+++ b/src/TestFile.txt
@@ -1,3 +1,3 @@
-Line 1
+Line 1 Modified
 Line 2
 Line 3
'''
        result = git_manager.apply_patch(self.repo_path, patch_text)

        self.assertTrue(result['applied'])
        with open(self.test_file) as f:
            content = f.read()
        self.assertIn('Line 1 Modified', content)

    def test_apply_patch_json_creates_new_file(self):
        patch_obj = {
            'files': [
                {
                    'path': 'src/NewFile.java',
                    'patched_content': 'public class NewFile {}'
                }
            ]
        }
        patch_text = json.dumps(patch_obj)
        result = git_manager.apply_patch(self.repo_path, patch_text)

        self.assertTrue(result['applied'])
        new_file = os.path.join(self.repo_path, 'src', 'NewFile.java')
        self.assertTrue(os.path.exists(new_file))
        with open(new_file) as f:
            self.assertIn('NewFile', f.read())

    def test_apply_patch_json_invalid_format(self):
        patch_text = '{ invalid json }'
        result = git_manager.apply_patch(self.repo_path, patch_text)

        self.assertFalse(result['applied'])
        self.assertTrue(len(result['errors']) > 0)

    def test_detect_repo_root(self):
        subdir = os.path.join(self.repo_path, 'src')
        detected = git_manager.detect_repo_root(subdir)
        self.assertEqual(os.path.normpath(detected), os.path.normpath(self.repo_path))


if __name__ == '__main__':
    unittest.main()

