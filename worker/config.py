"""Single home for tunables. No magic strings anywhere else in the worker."""

from __future__ import annotations

import os

# --- Model -------------------------------------------------------------------
# Pinned explicitly, not a floating alias. Resolved Aug 27 2026 against
# publishers/google/models in us-central1 for project pr-review-agent-ajr:
# 3.5-flash, 3.6-flash and 3.7-flash are GA there, and no Pro exists at 3.5+.
# Chose the newest GA model that meets the PRD's "Gemini 3.5+" requirement.
#
# Listing publisher models is NOT a reliable availability check — the global
# list happily returns models a given project/region cannot call. The only
# trustworthy probe is actually invoking it:
#   curl -H "Authorization: Bearer $(gcloud auth print-access-token)" \
#     -H "Content-Type: application/json" \
#     "https://aiplatform.googleapis.com/v1/projects/$GCP_PROJECT/locations/global/publishers/google/models/$MODEL:generateContent" \
#     -d '{"contents":[{"role":"user","parts":[{"text":"hi"}]}]}'
# No default, deliberately. A plausible-but-wrong id deploys clean and fails at
# the first Vertex call; build_agent raises with instructions instead. A default
# here silently pre-empted that error and made the check unreachable.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "")

# "global", not a region. Verified Aug 27 2026: every Gemini 3.x model 404s on
# us-central1 for this project while all of them answer on the global endpoint;
# only 2.5 is served regionally. Setting this to the Cloud Run region is the
# mistake that produces "Publisher model ... was not found".
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "global")

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

    The slash in "owner/name" must be escaped: Firestore reads "/" as a path
    separator, so an unescaped repo name splits the id into collection/document
    segments and raises "A document must have an even number of path elements".
    """
    return f"{repo.replace('/', '_')}:{pr_number}:{head_sha}"
