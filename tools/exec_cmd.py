"""Execute external commands with proper error handling, timeout support, and command whitelisting."""

import logging
import os
import subprocess
from typing import List, Dict, Any, Iterable

logger = logging.getLogger(__name__)


def _normalize_command_name(command: str) -> str:
    return os.path.basename(str(command or '').strip()).lower()


def _normalize_allowed_commands(allowed_commands: Iterable[str]) -> set:
    return {_normalize_command_name(cmd) for cmd in (allowed_commands or []) if _normalize_command_name(cmd)}


def _is_command_allowed(command: str, allowed_commands: Iterable[str]) -> bool:
    allowed = _normalize_allowed_commands(allowed_commands)
    return _normalize_command_name(command) in allowed


def run(cmd_list: List[str], cwd: str = None, timeout: int = None, allowed_commands: Iterable[str] = None) -> Dict[str, Any]:
    """Run command and capture stdout/stderr.
    
    Args:
        cmd_list: Command as list of strings (e.g., ['git', 'status']).
        cwd: Work directory to run command in. If None, use current directory.
        timeout: Timeout in seconds. If exceeded, process is killed.
        allowed_commands: Optional iterable of allowed executable names.
        
    Returns:
        Dict with keys: {code, stdout, stderr}
        code: Exit code (0 for success, -1 for error)
        stdout: Standard output
        stderr: Standard error
    """
    try:
        if allowed_commands is not None and not _is_command_allowed(cmd_list[0], allowed_commands):
            blocked = _normalize_command_name(cmd_list[0])
            logger.warning(f"Command blocked by whitelist: {blocked}")
            return {
                "code": -1,
                "stdout": "",
                "stderr": f"Command blocked by whitelist: {blocked}",
            }

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

