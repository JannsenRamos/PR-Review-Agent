"""Read-side agent tools. Every one returns a dict; failures come back as
{"error": ...} so the agent can see them and route around them.
"""

from __future__ import annotations

import base64
import logging

import httpx
from config import MAX_DIFF_BYTES
from github_auth import GITHUB_API, api_headers

log = logging.getLogger("worker.github_read")

GUIDELINE_PATHS = ("CONTRIBUTING.md", "docs/CONTRIBUTING.md", ".github/CONTRIBUTING.md")
LINT_CONFIG_PATHS = (".eslintrc.json", ".ruff.toml", "pyproject.toml", "setup.cfg", ".flake8")


def _get(path: str, installation_id, **kwargs) -> httpx.Response:
    return httpx.get(f"{GITHUB_API}{path}", headers=api_headers(installation_id), timeout=20.0, **kwargs)


def fetch_diff(repo: str, pr_number: int, installation_id=None) -> dict:
    """Changed files with their patches.

    Oversized diffs are truncated and flagged rather than silently skimmed — a
    shallow pass that reads like a real review is worse than an honest escalation.
    """
    try:
        files, page = [], 1
        while True:
            response = _get(
                f"/repos/{repo}/pulls/{pr_number}/files",
                installation_id,
                params={"per_page": 100, "page": page},
            )
            response.raise_for_status()
            batch = response.json()
            if not batch:
                break
            for f in batch:
                files.append(
                    {
                        "path": f["filename"],
                        "status": f["status"],
                        "patch": f.get("patch"),
                        "additions": f["additions"],
                        "deletions": f["deletions"],
                    }
                )
            if len(batch) < 100:
                break
            page += 1

        size = sum(len(f["patch"] or "") for f in files)
        truncated = size > MAX_DIFF_BYTES
        return {"files": files, "truncated": truncated, "total_patch_bytes": size}
    except Exception as exc:
        log.exception("fetch_diff failed")
        return {"error": str(exc), "files": []}


def fetch_file_context(repo: str, path: str, ref: str, start_line: int, end_line: int, installation_id=None) -> dict:
    """Surrounding source for a changed region, so findings can be grounded."""
    try:
        response = _get(f"/repos/{repo}/contents/{path}", installation_id, params={"ref": ref})
        response.raise_for_status()
        body = response.json()
        if body.get("encoding") != "base64":
            return {"error": f"unexpected encoding {body.get('encoding')}", "path": path}
        text = base64.b64decode(body["content"]).decode("utf-8", errors="replace")
        all_lines = text.splitlines()
        lo = max(0, start_line - 1)
        hi = min(len(all_lines), end_line)
        return {"path": path, "start_line": lo + 1, "lines": all_lines[lo:hi]}
    except Exception as exc:
        log.exception("fetch_file_context failed")
        return {"error": str(exc), "path": path}


def fetch_guidelines(repo: str, ref: str, installation_id=None) -> dict:
    """The conventions a finding can be cited against.

    Without at least one of these the convention checker has nothing to quote,
    and every convention finding is low-confidence by definition.
    """
    result: dict = {"contributing": None, "lint_config": None}
    try:
        for candidate in GUIDELINE_PATHS:
            got = fetch_file_context(repo, candidate, ref, 1, 400, installation_id)
            if "error" not in got:
                result["contributing"] = "\n".join(got["lines"])
                result["contributing_path"] = candidate
                break
        for candidate in LINT_CONFIG_PATHS:
            got = fetch_file_context(repo, candidate, ref, 1, 200, installation_id)
            if "error" not in got:
                result["lint_config"] = "\n".join(got["lines"])
                result["lint_config_path"] = candidate
                break
        return result
    except Exception as exc:
        log.exception("fetch_guidelines failed")
        return {"error": str(exc), **result}


def fetch_ci_status(repo: str, sha: str, installation_id=None) -> dict:
    """Read CI. Never re-runs it — see the PRD's non-goals."""
    failing = {"failure", "timed_out", "cancelled", "action_required"}
    try:
        response = _get(f"/repos/{repo}/commits/{sha}/check-runs", installation_id)
        response.raise_for_status()
        checks = [
            {"name": r.get("name"), "status": r.get("status"), "conclusion": r.get("conclusion")}
            for r in response.json().get("check_runs", [])
        ]
        if any(c["conclusion"] in failing for c in checks):
            state = "failure"
        elif any(c["status"] != "completed" for c in checks):
            state = "pending"
        else:
            state = "success"
        return {"state": state, "checks": checks}
    except Exception as exc:
        log.exception("fetch_ci_status failed")
        return {"error": str(exc), "state": "unknown", "checks": []}
