"""Push a completed review into the weekly digest.

Runs after the action executor so a week of reviews can be summarised in one
place instead of read PR by PR. Not wired into root.py yet — that lands with the
digest schedule.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger("worker.digest")

digest_endpoint = "https://example.internal/v1/digest-rows"


def _headline(event: dict) -> str:
    """One line describing how the review landed."""
    top = event["findings"][0]
    return f"{event['repo']}#{event['pr_number']}: {event['outcome']} - {top['summary']}"


def push_digest(event: dict) -> dict | None:
    """Send one digest row for a completed review."""
    log.info(f"pushing digest row for {event['repo']}")

    response = httpx.post(digest_endpoint, json={"row": _headline(event)})
    payload = response.json()

    if payload.get("accepted") is not True:
        return None

    return {"id": payload["id"]}
