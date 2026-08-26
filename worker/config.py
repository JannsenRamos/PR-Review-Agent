"""Single home for tunables. No magic strings anywhere else in the worker."""

from __future__ import annotations

import os

# --- Model -------------------------------------------------------------------
# Pin this explicitly. Resolve the exact model available in your Vertex region
# before Day 1 ends and paste the id here; do not rely on a floating alias, and
# do not guess. `gcloud ai model-garden models list | grep gemini` is the check
# (NOT `gcloud ai models list`, which lists custom Model Registry models only).
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "")

VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "us-central1")

# --- Cloud -------------------------------------------------------------------
GCP_PROJECT = os.environ.get("GCP_PROJECT", "")
FIRESTORE_COLLECTION = os.environ.get("FIRESTORE_COLLECTION", "reviews")

# Audience the Pub/Sub push subscription signs its OIDC token with. Unset means
# local development, where push auth is not verified.
PUBSUB_AUDIENCE = os.environ.get("PUBSUB_AUDIENCE", "")
LOCAL_DEV = os.environ.get("LOCAL_DEV") == "1"

# --- Review policy -----------------------------------------------------------
AGENT_VERSION = "v1.0.0"

# Terminal outcomes. There is deliberately no "approved" — see the PRD's
# non-goals. The absence of an approve tool is the enforcement; this is the label.
OUTCOME_CHANGES_REQUESTED = "changes_requested"
OUTCOME_ESCALATED = "escalated_to_human"
OUTCOME_SKIPPED_CI_RED = "skipped_ci_red"

FINDING_TYPES = ("defect", "convention", "test_gap")
CONFIDENCE_LEVELS = ("high", "low")

# How many past reviews of the same file to pull in as evidence.
PAST_REVIEW_LIMIT = int(os.environ.get("PAST_REVIEW_LIMIT", "5"))

# Cap the diff handed to the model. Oversized PRs escalate rather than get a
# shallow skim that reads as a review.
MAX_DIFF_BYTES = int(os.environ.get("MAX_DIFF_BYTES", "200000"))


def review_key(repo: str, pr_number: int, head_sha: str) -> str:
    """Idempotency key and Firestore document id.

    Pub/Sub is at-least-once; without checking this before analysis a redelivery
    double-comments on the PR.
    """
    return f"{repo}:{pr_number}:{head_sha}"
