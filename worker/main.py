"""Pub/Sub push endpoint. All latency, retries and dead-lettering live here.

Return codes matter:
  200 -> ack. Includes a completed review AND an honest escalation.
  500 -> nack, Pub/Sub redelivers (max 5, then dead-letter).
A model failure is not a 500. It is a completed review with outcome
"escalated_to_human" — retrying it would just burn the same tokens again.
"""

from __future__ import annotations

import base64
import json
import logging

from fastapi import FastAPI, Header, Request, Response

from config import (
    LOCAL_DEV,
    OUTCOME_ESCALATED,
    OUTCOME_SKIPPED_CI_RED,
    PUBSUB_AUDIENCE,
    review_key,
)
from tools.github_read import fetch_ci_status
from tools.github_write import post_summary_comment
from tools.memory import already_reviewed, write_review_event

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("worker")

app = FastAPI(title="pr-review-worker")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def verify_push_token(authorization: str | None) -> bool:
    """Cloud Run is deployed private, but verify the OIDC audience anyway."""
    if LOCAL_DEV or not PUBSUB_AUDIENCE:
        return True
    if not authorization or not authorization.startswith("Bearer "):
        return False
    try:
        from google.auth.transport import requests as g_requests
        from google.oauth2 import id_token

        id_token.verify_oauth2_token(
            authorization.removeprefix("Bearer "), g_requests.Request(), PUBSUB_AUDIENCE
        )
        return True
    except Exception:
        log.warning("rejected push: bad OIDC token")
        return False


@app.post("/jobs")
async def jobs(request: Request, authorization: str | None = Header(default=None)) -> Response:
    if not verify_push_token(authorization):
        return Response(status_code=401)

    envelope = await request.json()
    try:
        job = json.loads(base64.b64decode(envelope["message"]["data"]).decode())
    except Exception:
        # Malformed message: acking is correct, redelivery cannot fix it.
        log.exception("undecodable envelope, dropping")
        return Response(status_code=200)

    repo, pr_number, head_sha = job["repo"], job["pr_number"], job["head_sha"]
    installation_id = job.get("installation_id")
    key = review_key(repo, pr_number, head_sha)

    try:
        if already_reviewed(repo, pr_number, head_sha):
            log.info("skip %s: already reviewed", key)
            return Response(status_code=200)

        ci_state = "success"
        if job.get("deferred"):
            # Admitted with CI still running; look once more before spending a run.
            ci = fetch_ci_status(repo, head_sha, installation_id)
            ci_state = ci["state"]
            if ci_state == "failure":
                post_summary_comment(
                    repo,
                    pr_number,
                    f"CI went red on `{head_sha[:7]}` while this was queued — skipping review.",
                    installation_id,
                )
                write_review_event(repo, pr_number, head_sha, [], OUTCOME_SKIPPED_CI_RED, ci_state)
                return Response(status_code=200)

        run_review(repo, pr_number, head_sha, installation_id, ci_state)
        return Response(status_code=200)

    except Exception as exc:
        # Transient (GitHub 5xx, Firestore unavailable) -> let Pub/Sub retry.
        log.exception("job %s failed, nacking for retry", key)
        del exc
        return Response(status_code=500)


def run_review(repo: str, pr_number: int, head_sha: str, installation_id, ci_state: str) -> None:
    """Seam where the ADK agent plugs in (Day 1 step 6 / Day 2).

    Until GEMINI_MODEL is pinned and the agent is wired, this closes the pipe
    end to end so the plumbing can be proven independently of the model.
    """
    try:
        from agent.root import review_pull_request
    except ImportError:
        post_summary_comment(
            repo,
            pr_number,
            f"PR Review Agent reached `{head_sha[:7]}` — pipeline is live, agent not yet wired.",
            installation_id,
        )
        write_review_event(repo, pr_number, head_sha, [], OUTCOME_ESCALATED, ci_state)
        log.info("posted placeholder for %s#%s", repo, pr_number)
        return

    review_pull_request(repo, pr_number, head_sha, installation_id, ci_state)
