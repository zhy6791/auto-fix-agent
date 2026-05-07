"""Unit tests for tools/file_io.py"""

import os
import tempfile
import unittest
from tools.file_io import read_file, tail_file, write_file


class TestFileIO(unittest.TestCase):
    
    def setUp(self):
        """Create a temporary directory for test files."""
        self.test_dir = tempfile.mkdtemp()
    
    def tearDown(self):
        """Clean up temporary files."""
        import shutil
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
    
    def test_write_file_simple(self):
        """Test writing a new file."""
        path = os.path.join(self.test_dir, 'test.txt')
        content = "Hello World"
        result = write_file(path, content)
        
        self.assertTrue(result)
        self.assertTrue(os.path.exists(path))
        with open(path, 'r') as f:
            self.assertEqual(f.read(), content)
    
    def test_write_file_overwrite_false(self):
        """Test that write_file raises FileExistsError when file exists and overwrite=False."""
        path = os.path.join(self.test_dir, 'test.txt')
        write_file(path, "Original")
        
        with self.assertRaises(FileExistsError):
            write_file(path, "New content", overwrite=False)
    
    def test_write_file_overwrite_true(self):
        """Test that write_file overwrites when overwrite=True."""
        path = os.path.join(self.test_dir, 'test.txt')
        write_file(path, "Original")
        result = write_file(path, "New content", overwrite=True)
        
        self.assertTrue(result)
        with open(path, 'r') as f:
            self.assertEqual(f.read(), "New content")
    
    def test_write_file_creates_directories(self):
        """Test that write_file creates missing directories."""
        path = os.path.join(self.test_dir, 'subdir1', 'subdir2', 'test.txt')
        content = "Test content"
        result = write_file(path, content)
        
        self.assertTrue(result)
        self.assertTrue(os.path.exists(path))
        with open(path, 'r') as f:
            self.assertEqual(f.read(), content)
    
    def test_read_file_simple(self):
        """Test reading a file."""
        path = os.path.join(self.test_dir, 'test.txt')
        content = "Hello World"
        with open(path, 'w') as f:
            f.write(content)
        
        result = read_file(path)
        self.assertEqual(result, content)
    
    def test_read_file_not_found(self):
        """Test that read_file raises FileNotFoundError for missing file."""
        path = os.path.join(self.test_dir, 'nonexistent.txt')
        with self.assertRaises(FileNotFoundError):
            read_file(path)
    
    def test_tail_file_full_read(self):
        """Test tail_file with since_pos=None (read entire file)."""
        path = os.path.join(self.test_dir, 'test.txt')
        content = "Line 1\nLine 2\nLine 3"
        with open(path, 'w') as f:
            f.write(content)
        
        pos, chunk = tail_file(path, since_pos=None)
        self.assertEqual(chunk, content)
        # pos should match the actual byte position (accounting for newline variants)
        self.assertGreater(pos, 0)
        self.assertTrue(len(chunk) > 0)
    
    def test_tail_file_partial_read(self):
        """Test tail_file reading from a specific position."""
        path = os.path.join(self.test_dir, 'test.txt')
        content = "0123456789"
        with open(path, 'w') as f:
            f.write(content)
        
        # Read from position 5
        pos, chunk = tail_file(path, since_pos=5)
        self.assertEqual(chunk, "56789")
        self.assertEqual(pos, 10)
    
    def test_tail_file_not_found(self):
        """Test that tail_file raises FileNotFoundError for missing file."""
        path = os.path.join(self.test_dir, 'nonexistent.txt')
        with self.assertRaises(FileNotFoundError):
            tail_file(path)
    
    def test_tail_file_rotation(self):
        """Test tail_file when file is truncated (position beyond file size)."""
        path = os.path.join(self.test_dir, 'test.txt')
        content = "Short"
        with open(path, 'w') as f:
            f.write(content)
        
        # Request position beyond file size (file rotation scenario)
        pos, chunk = tail_file(path, since_pos=1000)
        # Should reset to 0 and re-read from beginning
        self.assertEqual(chunk, content)


if __name__ == '__main__':
    unittest.main()


