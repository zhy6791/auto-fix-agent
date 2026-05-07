"""Unit tests for tools/exec_cmd.py"""

import unittest
import sys
from tools.exec_cmd import run


class TestExecCmd(unittest.TestCase):
    
    def test_run_success(self):
        """Test running a successful command."""
        result = run(["python", "-c", "print('hello')"])
        
        self.assertEqual(result["code"], 0)
        self.assertIn("hello", result["stdout"])
        self.assertEqual(result["stderr"], "")
    
    def test_run_failure(self):
        """Test running a command that fails."""
        result = run(["python", "-c", "import nonexistent_module"])
        
        self.assertNotEqual(result["code"], 0)
        self.assertIn("ModuleNotFoundError", result["stderr"])
    
    def test_run_with_cwd(self):
        """Test running command with specific working directory."""
        import tempfile
        import os
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a test file in the temp directory
            test_file = os.path.join(tmpdir, "test.txt")
            with open(test_file, 'w') as f:
                f.write("test")
            
            # Use python to list files (more portable)
            result = run(["python", "-c", "import os; print(os.listdir('.')[:1])"], cwd=tmpdir)
            
            self.assertEqual(result["code"], 0)
            self.assertIn("test.txt", result["stdout"])
    
    def test_run_not_found(self):
        """Test running a non-existent command."""
        result = run(["nonexistent_command_xyz_123"])
        
        self.assertEqual(result["code"], -1)
        self.assertIn("not found", result["stderr"].lower())
    
    def test_run_timeout(self):
        """Test command timeout."""
        result = run(["python", "-c", "import time; time.sleep(10)"], timeout=1)
        
        self.assertEqual(result["code"], -1)
        self.assertIn("Timeout", result["stderr"])


if __name__ == '__main__':
    unittest.main()


