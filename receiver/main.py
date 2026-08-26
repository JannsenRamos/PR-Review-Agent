"""Webhook receiver: verify, admit, publish. Nothing slow happens here.

Contract (PRD/implementation-plan-v1.md section 2.1):
  - verify HMAC, filter events, gate on CI, publish to Pub/Sub, return 200
  - no LLM call and no diff fetch on this path; p95 target < 1s
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os

import httpx
from fastapi import FastAPI, Header, Request, Response

from github_auth import GITHUB_API, api_headers

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("receiver")

app = FastAPI(title="pr-review-receiver")

REVIEWED_ACTIONS = {"opened", "synchronize", "reopened"}
FAILING_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required"}

GCP_PROJECT = os.environ.get("GCP_PROJECT", "")
PUBSUB_TOPIC = os.environ.get("PUBSUB_TOPIC", "pr-review-jobs")

_publisher = None
_topic_path = None


def publisher():
    """Lazily build the Pub/Sub client so local replay runs without credentials."""
    global _publisher, _topic_path
    if _publisher is None:
        from google.cloud import pubsub_v1

        _publisher = pubsub_v1.PublisherClient()
        _topic_path = _publisher.topic_path(GCP_PROJECT, PUBSUB_TOPIC)
    return _publisher, _topic_path


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def verify_signature(body: bytes, header: str | None) -> bool:
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET")
    if not secret:
        raise RuntimeError("GITHUB_WEBHOOK_SECRET is not set")
    if not header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def fetch_ci_state(repo: str, sha: str, installation_id: int | None) -> dict:
    """Collapse check-runs and legacy commit statuses into one verdict.

    A repo with no CI at all must not be blocked forever, so "nothing ran" is
    reported as success with no_checks set, and the caller decides.
    """
    headers = api_headers(installation_id)
    checks: list[dict] = []
    state = "success"

    with httpx.Client(timeout=10.0, headers=headers) as client:
        runs = client.get(f"{GITHUB_API}/repos/{repo}/commits/{sha}/check-runs")
        runs.raise_for_status()
        for run in runs.json().get("check_runs", []):
            checks.append(
                {"name": run.get("name"), "status": run.get("status"), "conclusion": run.get("conclusion")}
            )

        # Many repos still report through the legacy combined status API.
        combined = client.get(f"{GITHUB_API}/repos/{repo}/commits/{sha}/status")
        combined.raise_for_status()
        combined_state = combined.json().get("state", "pending")
        combined_count = combined.json().get("total_count", 0)

    if any(c["conclusion"] in FAILING_CONCLUSIONS for c in checks):
        state = "failure"
    elif any(c["status"] != "completed" for c in checks):
        state = "pending"
    elif combined_count and combined_state == "failure":
        state = "failure"
    elif combined_count and combined_state == "pending":
        state = "pending"

    return {"state": state, "checks": checks, "no_checks": not checks and not combined_count}


def post_halt_comment(repo: str, pr_number: int, sha: str, installation_id: int | None) -> None:
    body = (
        f"CI is red on `{sha[:7]}` — skipping review until it's green. "
        "Reviewing broken code wastes the run; push a fix and I'll pick it up automatically."
    )
    httpx.post(
        f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments",
        headers=api_headers(installation_id),
        json={"body": body},
        timeout=10.0,
    ).raise_for_status()


@app.post("/webhook")
async def webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
) -> Response:
    body = await request.body()

    if not verify_signature(body, x_hub_signature_256):
        log.warning("rejected delivery %s: bad signature", x_github_delivery)
        return Response(status_code=401)

    if x_github_event != "pull_request":
        return Response(status_code=204)

    payload = json.loads(body)
    action = payload.get("action")
    if action not in REVIEWED_ACTIONS:
        return Response(status_code=204)

    pr = payload["pull_request"]
    if pr.get("draft"):
        log.info("skipping draft PR")
        return Response(status_code=204)

    repo = payload["repository"]["full_name"]
    pr_number = pr["number"]
    head_sha = pr["head"]["sha"]
    installation_id = (payload.get("installation") or {}).get("id")

    ci = fetch_ci_state(repo, head_sha, installation_id)
    if ci["state"] == "failure":
        post_halt_comment(repo, pr_number, head_sha, installation_id)
        log.info("halted %s#%s: CI red", repo, pr_number)
        return Response(status_code=200)

    job = {
        "delivery_id": x_github_delivery,
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "installation_id": installation_id,
        "action": action,
        # Pending CI is admitted and re-checked once by the worker rather than
        # requiring a scheduler to wake this back up.
        "deferred": ci["state"] == "pending",
    }

    client, topic = publisher()
    future = client.publish(
        topic,
        json.dumps(job).encode(),
        repo=repo,
        pr_number=str(pr_number),
    )
    log.info("published %s#%s sha=%s msg=%s", repo, pr_number, head_sha[:7], future.result(timeout=10))

    return Response(status_code=200)
