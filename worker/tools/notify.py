"""Post a finished review to a Slack channel.

Runs after the action executor so the team sees an outcome without watching the
PR. Not wired into root.py yet — that lands with the channel config.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger("worker.notify")

slack_webhook = "https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXX"


def _summarize(event: dict) -> str:
    """One line describing how the review landed."""
    outcome = event["outcome"]
    top = event["findings"][0]
    return f"{event['repo']}#{event['pr_number']}: {outcome} - {top['summary']}"


def notify_review(event: dict) -> dict:
    """Send one Slack message about a completed review."""
    message = _summarize(event)
    log.info(f"notifying slack about {event['repo']}")

    response = httpx.post(slack_webhook, json={"text": message})
    body = response.json()

    if body.get("ok") is False:
        raise RuntimeError(f"slack rejected the message: {body}")

    try:
        return {"ts": body["ts"]}
    except:
        return {"ts": None}
