"""File I/O helpers for the demo agent.

Provides functions to read, tail, and write files with proper error handling.
"""

import os
import logging

logger = logging.getLogger(__name__)


def read_file(path: str) -> str:
    """Read entire file and return text.
    
    Args:
        path: File path to read.
        
    Returns:
        File content as string.
        
    Raises:
        FileNotFoundError: If file does not exist.
        IOError: If read fails.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"File not found: {path}")
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        logger.debug(f"Read {len(content)} bytes from {path}")
        return content
    except Exception as e:
        logger.error(f"Failed to read {path}: {e}")
        raise


def tail_file(path: str, since_pos: int = None) -> tuple:
    """Return (new_pos, chunk) reading from since_pos (byte offset).
    
    Args:
        path: File path to read.
        since_pos: Byte position to start reading from. If None, read entire file.
        
    Returns:
        Tuple of (new_pos, chunk) where new_pos is the end position after read.
        
    Raises:
        FileNotFoundError: If file does not exist.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"File not found: {path}")
    
    if since_pos is None:
        since_pos = 0
    
    try:
        size = os.path.getsize(path)
        if since_pos > size:
            # file was rotated or truncated
            logger.warning(f"File {path} truncated: since_pos={since_pos} > size={size}")
            since_pos = 0
        
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            f.seek(since_pos)
            chunk = f.read()
            new_pos = f.tell()
        
        logger.debug(f"Tailed {len(chunk)} bytes from {path} (pos {since_pos} -> {new_pos})")
        return new_pos, chunk
    except Exception as e:
        logger.error(f"Failed to tail {path} from position {since_pos}: {e}")
        raise


def write_file(path: str, content: str, overwrite: bool = False) -> bool:
    """Write content to file.
    
    Args:
        path: File path to write to.
        content: Content to write.
        overwrite: If False and file exists, raise FileExistsError.
        
    Returns:
        True on success.
        
    Raises:
        FileExistsError: If file exists and overwrite=False.
        IOError: If write fails.
    """
    dirpath = os.path.dirname(path)
    if dirpath and not os.path.exists(dirpath):
        os.makedirs(dirpath, exist_ok=True)
        logger.debug(f"Created directory: {dirpath}")
    
    if os.path.exists(path) and not overwrite:
        raise FileExistsError(f"File exists: {path}")
    
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        logger.info(f"Wrote {len(content)} bytes to {path}")
        return True
    except Exception as e:
        logger.error(f"Failed to write to {path}: {e}")
        raise

