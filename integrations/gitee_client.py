"""Gitee API client for PR creation."""

import logging
import requests

logger = logging.getLogger(__name__)


class GiteeClient:
    def __init__(self, access_token, base_url="https://gitee.com/api/v5", timeout=30):
        self.access_token = access_token
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout

    def create_pull_request(self, owner, repo, title, head, base,
                            body="", prune_source_branch=False):
        """Create a Pull Request on Gitee.

        POST /repos/{owner}/{repo}/pulls

        Returns dict: {success: bool, url: str, number: int, error: str}
        """
        url = f"{self.base_url}/repos/{owner}/{repo}/pulls"
        payload = {
            "title": title,
            "head": head,
            "base": base,
            "body": body,
            "prune_source_branch": prune_source_branch,
        }
        params = {"access_token": self.access_token}

        try:
            resp = requests.post(url, json=payload, params=params,
                                timeout=self.timeout)
            if resp.status_code == 201:
                data = resp.json()
                return {
                    "success": True,
                    "url": data.get("html_url", ""),
                    "number": data.get("number"),
                    "error": None,
                }
            else:
                error_msg = f"HTTP {resp.status_code}"
                try:
                    error_detail = resp.json()
                    error_msg += f": {error_detail.get('message', resp.text)}"
                except Exception:
                    error_msg += f": {resp.text[:200]}"
                logger.error("Gitee PR creation failed: %s", error_msg)
                return {"success": False, "url": "", "number": None, "error": error_msg}
        except requests.exceptions.Timeout:
            return {"success": False, "url": "", "number": None, "error": "Request timeout"}
        except requests.exceptions.ConnectionError as e:
            return {"success": False, "url": "", "number": None, "error": f"Connection error: {e}"}
        except Exception as e:
            logger.exception("Unexpected error creating Gitee PR")
            return {"success": False, "url": "", "number": None, "error": str(e)}

    def get_repo_info(self, owner, repo):
        """Get repository info to verify access and get default branch.

        Returns dict: {success: bool, default_branch: str, error: str}
        """
        url = f"{self.base_url}/repos/{owner}/{repo}"
        params = {"access_token": self.access_token}
        try:
            resp = requests.get(url, params=params, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "success": True,
                    "default_branch": data.get("default_branch", "master"),
                    "error": None,
                }
            else:
                return {"success": False, "default_branch": "",
                        "error": f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"success": False, "default_branch": "", "error": str(e)}
