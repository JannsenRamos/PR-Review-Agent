"""Firestore memory: idempotency, past-review retrieval, review event writes.

The review document is a public contract — a standalone, self-describing event
that a future consumer can read cold. See infra/firestore-schema.md before
changing its shape.
"""

from __future__ import annotations

import datetime as dt
import logging

from config import AGENT_VERSION, FIRESTORE_COLLECTION, PAST_REVIEW_LIMIT, review_key

log = logging.getLogger("worker.memory")

_db = None


def db():
    global _db
    if _db is None:
        from google.cloud import firestore

        _db = firestore.Client()
    return _db


def already_reviewed(repo: str, pr_number: int, head_sha: str) -> bool:
    """True when this exact commit already has a completed review."""
    doc = db().collection(FIRESTORE_COLLECTION).document(review_key(repo, pr_number, head_sha)).get()
    return doc.exists


def fetch_past_reviews(repo: str, paths: list[str], limit: int = PAST_REVIEW_LIMIT) -> dict:
    """Past findings on the same files, as evidence for the convention checker.

    Needs a composite index on repo + findings.path; Firestore returns a console
    link to create it the first time this runs.
    """
    try:
        from google.cloud.firestore_v1.base_query import FieldFilter

        # FieldFilter, not positional args: the positional form is deprecated
        # and warns on every call.
        query = (
            db()
            .collection(FIRESTORE_COLLECTION)
            .where(filter=FieldFilter("repo", "==", repo))
            .order_by("timestamp", direction="DESCENDING")
            .limit(limit * 4)
        )
        wanted = set(paths)
        reviews = []
        for doc in query.stream():
            data = doc.to_dict()
            relevant = [f for f in data.get("findings", []) if f.get("path") in wanted]
            if relevant:
                reviews.append({**data, "findings": relevant})
            if len(reviews) >= limit:
                break
        return {"reviews": reviews}
    except Exception as exc:  # tools report failure, they do not raise
        log.exception("fetch_past_reviews failed")
        return {"error": str(exc), "reviews": []}


def write_review_event(
    repo: str,
    pr_number: int,
    head_sha: str,
    findings: list[dict],
    outcome: str,
    ci_state: str,
    decision: dict | None = None,
) -> dict:
    event = {
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "findings": findings,
        "outcome": outcome,
        "ci_state": ci_state,
        # How the outcome was reached: counts by disposition and finding type,
        # and why it escalated when it did. Additive to the documented shape - a
        # cold reader that only knows the original fields is unaffected.
        "decision": decision or {},
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "agent_version": AGENT_VERSION,
    }
    try:
        key = review_key(repo, pr_number, head_sha)
        db().collection(FIRESTORE_COLLECTION).document(key).set(event)
        return {"doc_id": key}
    except Exception as exc:
        log.exception("write_review_event failed")
        return {"error": str(exc)}
