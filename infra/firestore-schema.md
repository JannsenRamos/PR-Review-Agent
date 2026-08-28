# Firestore schema — `reviews`

**This document shape is a contract.** It is written as a standalone,
self-describing event so a future consumer (V2's task assigner) can read it
without knowing anything about V1's internals. Changing it is a breaking change.
No queue, API, or plugin interface exists for that consumer in V1 — just this
shape, documented.

Collection: `reviews`
Document ID: `{repo}:{pr_number}:{head_sha}` — also the idempotency key. Pub/Sub
delivers at-least-once; the worker checks for this document before analysing, so
a redelivered webhook produces no duplicate comments.

```json
{
  "repo": "owner/name",
  "pr_number": 42,
  "head_sha": "abc123",
  "findings": [
    {
      "type": "defect | convention | test_gap",
      "path": "src/api/handler.py",
      "line": 87,
      "summary": "Response body is read without checking status",
      "citation": "CONTRIBUTING.md: 'every outbound call must check status before parsing'",
      "confidence": "high | low",
      "posted_as": "inline | summary | suppressed",
      "comment_id": 1234567
    }
  ],
  "outcome": "changes_requested | escalated_to_human | skipped_ci_red",
  "decision": {
    "outcome": "changes_requested",
    "posted_as": { "inline": 2, "summary": 1, "suppressed": 1 },
    "by_type": { "defect": 1, "convention": 2, "test_gap": 1 },
    "suppressed_reasons": { "citation_not_grounded": 1 },
    "evidence_sources": 2,
    "escalation_reason": null
  },
  "ci_state": "success | failure | pending",
  "timestamp": "2026-08-26T12:00:00Z",
  "agent_version": "v1.0.0"
}
```

## Notes

- `confidence` is per-finding rather than a parallel `confidence[]` array. Same
  information as the PRD sketch, with no way for the two lists to drift apart.
- `posted_as: "suppressed"` records a finding the confidence gate dropped. Worth
  keeping: it is the raw material for V1's Tier 3 feedback loop and for judging
  whether the gate is set too tight.
- There is no `approved` outcome, and there never will be one.
- `decision` is the audit trail for *how* the outcome was reached, added after
  the first live run showed the event could say what happened but not why. It is
  additive: a consumer that only knows the original fields is unaffected.
- `decision.escalation_reason` is null on a completed review and otherwise names
  what stopped it — the agent's own phrase when it escalated deliberately, or one
  of `no_findings`, `all_findings_suppressed`, `no_conventions_to_cite`,
  `no_citable_findings`, `ci_red_before_review`, `agent_run_failed`. These call
  for different responses from the human picking the PR up, so a bare
  `escalated_to_human` was not enough to act on.
- `findings[].type` is written from the class the agent declares when it calls
  the tool. It was previously always `"unknown"`: the taxonomy lived in the
  prompt and nothing captured it.

## Index

`fetch_past_reviews` filters by `repo` and orders by `timestamp` descending.
Firestore will return a console link to create the composite index the first
time the query runs — create it then, on Day 3, not on demo day.
