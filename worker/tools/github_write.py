"""Write-side agent tools — the agent's only way to affect the world.

Note what is absent: there is no approve tool. "Never approves" is enforced by
the tool simply not existing, not by asking the model nicely.
"""

from __future__ import annotations

import logging

import httpx
from github_auth import GITHUB_API, api_headers

log = logging.getLogger("worker.github_write")

MIN_CITATION_CHARS = 12


def _post(path: str, installation_id, payload: dict) -> httpx.Response:
    return httpx.post(
        f"{GITHUB_API}{path}", headers=api_headers(installation_id), json=payload, timeout=20.0
    )


def post_inline_comment(
    repo: str,
    pr_number: int,
    commit_sha: str,
    path: str,
    line: int,
    body: str,
    citation: str,
    installation_id=None,
) -> dict:
    """Post one line-anchored comment. Requires a citation.

    The confidence gate lives here rather than only in the prompt: an inline
    comment claims certainty, and certainty has to be backed by a quoted rule
    from CONTRIBUTING.md / lint config or a prior review comment on this file.
    A prompt will drift under load; this check will not.

    Callers should fall back to the summary comment when this returns an error —
    including the 422 GitHub returns for a line outside the diff hunk.
    """
    if not citation or len(citation.strip()) < MIN_CITATION_CHARS:
        return {
            "error": "citation_required",
            "detail": (
                "Inline comments must cite a specific rule or prior review comment. "
                "Move this finding to the summary comment and phrase it as a question."
            ),
        }

    payload = {
        "body": f"{body}\n\n> Cited: {citation.strip()}",
        "commit_id": commit_sha,
        "path": path,
        "line": line,
        "side": "RIGHT",
    }
    try:
        response = _post(f"/repos/{repo}/pulls/{pr_number}/comments", installation_id, payload)
        if response.status_code == 422:
            return {"error": "line_not_in_diff", "path": path, "line": line, "detail": response.text[:300]}
        response.raise_for_status()
        return {"comment_id": response.json()["id"]}
    except Exception as exc:
        log.exception("post_inline_comment failed")
        return {"error": str(exc)}


def request_changes(repo: str, pr_number: int, summary_body: str, installation_id=None) -> dict:
    """Submit the review. One of only two terminal states."""
    try:
        response = _post(
            f"/repos/{repo}/pulls/{pr_number}/reviews",
            installation_id,
            {"body": summary_body, "event": "REQUEST_CHANGES"},
        )
        response.raise_for_status()
        return {"review_id": response.json()["id"]}
    except Exception as exc:
        log.exception("request_changes failed")
        return {"error": str(exc)}


def post_summary_comment(repo: str, pr_number: int, body: str, installation_id=None) -> dict:
    """The single home for every low-confidence observation, phrased as questions."""
    try:
        response = _post(f"/repos/{repo}/issues/{pr_number}/comments", installation_id, {"body": body})
        response.raise_for_status()
        return {"comment_id": response.json()["id"]}
    except Exception as exc:
        log.exception("post_summary_comment failed")
        return {"error": str(exc)}


def assign_reviewer(repo: str, pr_number: int, logins: list[str], installation_id=None) -> dict:
    """Escalation path: route to a human. The other terminal state."""
    try:
        response = _post(
            f"/repos/{repo}/pulls/{pr_number}/requested_reviewers", installation_id, {"reviewers": logins}
        )
        response.raise_for_status()
        return {"assigned": logins}
    except Exception as exc:
        log.exception("assign_reviewer failed")
        return {"error": str(exc)}


def apply_label(repo: str, pr_number: int, labels: list[str], installation_id=None) -> dict:
    try:
        response = _post(f"/repos/{repo}/issues/{pr_number}/labels", installation_id, {"labels": labels})
        response.raise_for_status()
        return {"labels": [lbl["name"] for lbl in response.json()]}
    except Exception as exc:
        log.exception("apply_label failed")
        return {"error": str(exc)}
