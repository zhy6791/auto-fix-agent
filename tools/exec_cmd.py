"""Execute external commands with proper error handling and timeout support."""

import subprocess
import logging
from typing import List, Dict, Any

logger = logging.getLogger(__name__)


def run(cmd_list: List[str], cwd: str = None, timeout: int = None) -> Dict[str, Any]:
    """Run command and capture stdout/stderr.
    
    Args:
        cmd_list: Command as list of strings (e.g., ['git', 'status']).
        cwd: Work directory to run command in. If None, use current directory.
        timeout: Timeout in seconds. If exceeded, process is killed.
        
    Returns:
        Dict with keys: {code, stdout, stderr}
        code: Exit code (0 for success, -1 for error)
        stdout: Standard output
        stderr: Standard error
    """
    try:
        logger.debug(f"Running: {' '.join(cmd_list)} (cwd={cwd})")
        proc = subprocess.run(
            cmd_list,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout
        )
        result = {
            "code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr
        }
        if proc.returncode == 0:
            logger.debug(f"Command succeeded")
        else:
            logger.warning(f"Command failed with exit code {proc.returncode}: {proc.stderr}")
        return result
    except subprocess.TimeoutExpired as e:
        logger.error(f"Command timeout after {timeout}s")
        return {
            "code": -1,
            "stdout": e.stdout or "",
            "stderr": f"Timeout ({timeout}s): {e}"
        }
    except FileNotFoundError as e:
        logger.error(f"Command not found: {cmd_list[0]}")
        return {"code": -1, "stdout": "", "stderr": f"Command not found: {e}"}
    except Exception as e:
        logger.error(f"Command execution failed: {e}")
        return {"code": -1, "stdout": "", "stderr": str(e)}

