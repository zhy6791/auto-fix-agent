"""Unit tests for tools/git_manager.py"""

import os
import shutil
import tempfile
import subprocess
import unittest
import time
from tools.git_manager import detect_repo_root, create_branch, apply_patch, parse_gitee_owner_repo


class TestGitManager(unittest.TestCase):
    
    def setUp(self):
        """Create a temporary git repo for testing."""
        self.test_dir = tempfile.mkdtemp()
        self.repo_path = self.test_dir
        
        # Initialize a git repo
        subprocess.check_call(["git", "init"], cwd=self.repo_path)
        subprocess.check_call(["git", "config", "user.name", "Test User"], cwd=self.repo_path)
        subprocess.check_call(["git", "config", "user.email", "test@example.com"], cwd=self.repo_path)
        
        # Create initial commit
        initial_file = os.path.join(self.repo_path, "README.md")
        with open(initial_file, 'w') as f:
            f.write("# Test Repo\n")
        subprocess.check_call(["git", "add", "README.md"], cwd=self.repo_path)
        subprocess.check_call(["git", "commit", "-m", "Initial commit"], cwd=self.repo_path)
    
    def tearDown(self):
        """Clean up temporary files with error handling for Windows permissions."""
        if os.path.exists(self.test_dir):
            # Close any open git processes first
            time.sleep(0.1)
            
            def handle_remove_error(func, path, exc):
                """Error handler for Windows permission issues."""
                import stat
                if not os.access(path, os.W_OK):
                    os.chmod(path, stat.S_IWUSR | stat.S_IREAD)
                    try:
                        func(path)
                    except Exception:
                        pass
            
            try:
                shutil.rmtree(self.test_dir, onerror=handle_remove_error)
            except Exception:
                pass
    
    def test_detect_repo_root_success(self):
        """Test detecting repo root from a subdirectory."""
        subdir = os.path.join(self.repo_path, "subdir")
        os.makedirs(subdir, exist_ok=True)
        
        detected = detect_repo_root(subdir)
        self.assertEqual(os.path.normpath(detected), os.path.normpath(self.repo_path))
    
    def test_detect_repo_root_failure(self):
        """Test detect_repo_root fails for non-git directory."""
        non_git_dir = tempfile.mkdtemp()
        try:
            with self.assertRaises(FileNotFoundError):
                detect_repo_root(non_git_dir)
        finally:
            shutil.rmtree(non_git_dir, ignore_errors=True)
    
    def test_detect_repo_root_from_root(self):
        """Test detecting repo root from repo itself."""
        detected = detect_repo_root(self.repo_path)
        self.assertEqual(os.path.normpath(detected), os.path.normpath(self.repo_path))
    
    def test_create_branch_success(self):
        """Test creating a new branch."""
        branch_name = "test-branch"
        result = create_branch(self.repo_path, branch_name)
        
        self.assertTrue(result)
        
        # Verify branch exists using git
        output = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=self.repo_path,
            universal_newlines=True
        )
        self.assertEqual(output.strip(), branch_name)
    
    def test_create_branch_duplicate(self):
        """Test creating a branch that already exists."""
        branch_name = "existing-branch"
        # Create first time
        create_branch(self.repo_path, branch_name)
        
        # Try to create again - should fail
        result = create_branch(self.repo_path, branch_name)
        self.assertFalse(result)
    
    def test_apply_patch_json_format(self):
        """Test applying a JSON-format patch."""
        import json
        
        # Create a test file
        test_file = os.path.join(self.repo_path, "src/main/java/TestFile.java")
        os.makedirs(os.path.dirname(test_file), exist_ok=True)
        
        patch_content = {
            "files": [
                {
                    "path": "src/main/java/TestFile.java",
                    "patched_content": "public class TestFile { /* patched */ }"
                }
            ]
        }
        patch_text = json.dumps(patch_content)
        
        result = apply_patch(self.repo_path, patch_text)
        
        self.assertTrue(result["applied"])
        self.assertIn("src/main/java/TestFile.java", result["files"])
        
        # Verify file was written
        self.assertTrue(os.path.exists(test_file))
        with open(test_file, 'r') as f:
            content = f.read()
        self.assertIn("patched", content)
    
    def test_apply_patch_json_invalid(self):
        """Test applying invalid JSON patch."""
        patch_text = "{ invalid json }"
        result = apply_patch(self.repo_path, patch_text)

        self.assertFalse(result["applied"])
        self.assertTrue(len(result["errors"]) > 0)

    def test_parse_gitee_owner_repo_https(self):
        owner, repo = parse_gitee_owner_repo('https://gitee.com/owner/repo.git')
        self.assertEqual(owner, 'owner')
        self.assertEqual(repo, 'repo')

    def test_parse_gitee_owner_repo_https_without_git(self):
        owner, repo = parse_gitee_owner_repo('https://gitee.com/owner/repo')
        self.assertEqual(owner, 'owner')
        self.assertEqual(repo, 'repo')

    def test_parse_gitee_owner_repo_ssh(self):
        owner, repo = parse_gitee_owner_repo('git@gitee.com:owner/repo.git')
        self.assertEqual(owner, 'owner')
        self.assertEqual(repo, 'repo')

    def test_parse_gitee_owner_repo_non_gitee(self):
        owner, repo = parse_gitee_owner_repo('https://github.com/owner/repo.git')
        self.assertIsNone(owner)
        self.assertIsNone(repo)


if __name__ == '__main__':
    unittest.main()


