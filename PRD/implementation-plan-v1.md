# Implementation Plan — V1 PR Review Agent

**Derived from:** `PRD/prd-v1-pr-review-agent.md`
**Written:** Aug 26, 2026 · **Ship by:** Aug 31, 2026, 5:00pm PDT (submit morning of Aug 31)
**Repo state at plan time:** empty — `main` has no commits.

---

## 0. Calendar reality check

The PRD's five-day plan starts today. Actual runway:

| PRD day | Date | Theme |
|---|---|---|
| Day 1 | **Tue Aug 26** (today) | Prove the pipe |
| Day 2 | Wed Aug 27 | Make it a real agent |
| Day 3 | Thu Aug 28 | Evidence + confidence gate · **cut checkpoint** |
| Day 4 | Fri Aug 29 | Harden and package |
| Day 5 | Sat Aug 30 | Record demo, write submission |
| Buffer | Sun Aug 31 AM | Submit (cutoff 5:00pm PDT) |

There is no slack. The Day-3 cut rule is the plan's safety valve — treat it as binding, not aspirational.

### Blockers to clear before writing code (~45 min, today)

1. **`gcloud` is not installed on this machine.** Everything in Day 1 depends on it. Install the Google Cloud CLI first, then `gcloud auth login` and `gcloud auth application-default login`.
2. ~~**Default `python` here is 3.14.2; ADK's supported range tops out lower.**~~ **Cleared Aug 26:** `.venv` is pinned to 3.12 via `py -3.12`, and `google-adk` 2.7.1 installs and imports there. Both Dockerfiles use `python:3.12-slim`.
3. **Pick the Gemini model ID and pin it.** The PRD says "Gemini 3.5+, version pinned explicitly." Resolve the exact available ID in your Vertex AI region at setup time and write it into `config.py` as a constant — do not let the SDK pick a floating alias.
4. **Target repo needs a `CONTRIBUTING.md`.** The convention checker has nothing to cite without one. Ten deliberate rules beat a generic template — each rule is a finding the agent can produce on camera.

---

## 1. Repo layout

```
PR-Review-Agent/
├── PRD/
│   ├── prd-v1-pr-review-agent.md
│   └── implementation-plan-v1.md
├── receiver/
│   ├── main.py                 # FastAPI: verify sig → CI gate → publish → 200
│   ├── github_auth.py          # App JWT → installation token (PAT fallback)
│   └── requirements.txt
├── worker/
│   ├── main.py                 # FastAPI: Pub/Sub push endpoint → run agent
│   ├── agent/
│   │   ├── root.py             # ADK orchestration: 3 sub-agents
│   │   ├── diff_analyzer.py
│   │   ├── convention_checker.py
│   │   ├── action_executor.py
│   │   └── prompts.py          # all instruction text, one file, easy to iterate
│   ├── tools/
│   │   ├── github_read.py      # fetch_diff, fetch_file_context, fetch_guidelines, fetch_ci_status
│   │   ├── github_write.py     # post_inline_comment, request_changes, assign_reviewer, apply_label
│   │   └── memory.py           # fetch_past_reviews, write_review_event
│   ├── config.py               # model ID, project, topic, thresholds — no magic strings elsewhere
│   └── requirements.txt
├── infra/
│   ├── deploy.sh               # idempotent: enable APIs, create topic/sub/DLQ, deploy both services
│   └── firestore-schema.md
├── scripts/
│   └── replay.py               # replay a saved webhook payload at localhost — the inner dev loop
├── fixtures/
│   └── pull_request.opened.json
├── Dockerfile.receiver
├── Dockerfile.worker
└── README.md                   # reproducible-from-zero; judged on this
```

Two Cloud Run services from one repo, two Dockerfiles. `github_auth.py` is copied into both images rather than packaged — at this timescale a shared library is a cost, not a saving.

---

## 2. Component specs

### 2.1 Receiver (Cloud Run, public, unauthenticated)

Single job: be fast and be right about admission.

1. `POST /webhook` — verify `X-Hub-Signature-256` HMAC against the webhook secret from Secret Manager. Mismatch → 401, body not logged.
2. Filter events: act on `pull_request` (`opened`, `synchronize`, `reopened`) only. Everything else → 204.
3. **CI gate.** Fetch check-runs for the head SHA.
   - Any conclusion `failure` / `timed_out` / `cancelled` → post one summary comment ("CI is red on `<sha>`; skipping review until it's green"), do **not** publish to Pub/Sub, return 200.
   - Still `in_progress` / `queued` → publish with `deferred: true`; the worker re-checks once before analyzing. Simpler than a scheduler, and honest on camera.
   - All green → publish.
4. Publish `{delivery_id, repo, pr_number, head_sha, installation_id, action}` to the topic. Return 200. **Target p95 < 1s** — no LLM call and no diff fetch on this path.

Idempotency key: `{repo}:{pr_number}:{head_sha}`. The worker checks Firestore for a completed review under that key and no-ops on a duplicate. Pub/Sub delivers at-least-once; without this, the demo double-comments.

### 2.2 Pub/Sub

- Topic `pr-review-jobs`, **push** subscription to the worker's `/jobs` endpoint — push, not pull, because Cloud Run scales to zero and push wakes it.
- Push auth: OIDC service-account token; the worker verifies the audience and rejects anything else. Worker deployed `--no-allow-unauthenticated`.
- Ack deadline 600s (LLM turnaround), `max_delivery_attempts: 5`, dead-letter topic `pr-review-dlq`.

### 2.3 Worker (Cloud Run, private)

`/jobs` decodes the envelope, checks the idempotency key, hands the job to the ADK root agent, and returns 200 on success or 500 to trigger redelivery on transient failure. A *model* failure (bad output, no findings) is not a 500 — it is a completed review with `outcome: "escalated_to_human"`.

Concurrency 1 per instance, `--memory 2Gi`, `--timeout 900`.

---

## 3. Agent design

The root agent orchestrates three sub-agents in sequence. Each sub-agent owns its tools and calls them itself — no application code calling tools on the agent's behalf. That distinction is an explicit judging criterion.

| Sub-agent | Input | Output into shared state | Tools |
|---|---|---|---|
| **Diff analyzer** | PR ref | `changed_files[]`, `new_branches[]`, `hunks[]` with line anchors | `fetch_diff`, `fetch_file_context` |
| **Convention checker** | analyzer output | `evidence[]` — rules and past comments keyed to file/line | `fetch_guidelines`, `fetch_past_reviews`, `fetch_ci_status` |
| **Action executor** | findings + evidence | GitHub artifacts + review event | `post_inline_comment`, `request_changes`, `assign_reviewer`, `apply_label`, `write_review_event` |

### Tool contracts — fix these on Day 1, everything else builds against them

```python
fetch_diff(repo: str, pr_number: int) -> {"files": [{"path", "patch", "additions", "deletions"}]}
fetch_file_context(repo, path, ref, start_line, end_line) -> {"path", "lines": [...]}
fetch_guidelines(repo, ref) -> {"contributing": str | None, "lint_config": str | None}
fetch_past_reviews(repo, paths: list[str], limit: int) -> {"reviews": [event_doc, ...]}
fetch_ci_status(repo, sha) -> {"state": "success" | "failure" | "pending", "checks": [...]}
post_inline_comment(repo, pr_number, commit_sha, path, line, body) -> {"comment_id"}
request_changes(repo, pr_number, summary_body) -> {"review_id"}
assign_reviewer(repo, pr_number, logins: list[str]) -> {"assigned": [...]}
apply_label(repo, pr_number, labels: list[str]) -> {"labels": [...]}
write_review_event(event: dict) -> {"doc_id"}
```

Every tool returns a dict with an `error` key on failure instead of raising — the agent has to be able to see a failure and route around it (inline comment rejected because the line falls outside the diff hunk → fall back to the summary comment).

### Confidence gate

Applied by the action executor, stated in its instruction *and* enforced in the tool wrapper:

- **High** — the finding names a concrete file + line **and** cites either a quoted rule from `CONTRIBUTING.md` / lint config or a prior review comment on the same file → inline comment, assertive phrasing.
- **Low** — anything else, including "this looks wrong but I can't cite it" → collected into **one** summary comment, phrased as a question.
- The wrapper rejects any `post_inline_comment` whose body carries no citation. Guardrail in code, not only in the prompt: the prompt will drift under load, the wrapper won't.

Terminal states only: `changes_requested` or `escalated_to_human`. **No approve path exists in the tool surface** — the tool simply isn't there to call.

### Findings taxonomy (what the analyzer is told to look for)

1. Local defects — unhandled errors, null/None paths, unclosed resources, injection risk.
2. Convention violations — cited against a stated rule or a prior comment.
3. New branches or error paths with no corresponding test.

Anything outside these three is not a finding. Product-fit judgments are ruled out in the prompt explicitly.

---

## 4. Firestore schema (forward-compatible — the one V2 concession)

Collection `reviews`, document ID `{repo}:{pr_number}:{head_sha}`:

```json
{
  "repo": "owner/name",
  "pr_number": 42,
  "head_sha": "abc123",
  "findings": [
    {
      "type": "convention|defect|test_gap",
      "path": "src/api/handler.py",
      "line": 87,
      "summary": "...",
      "citation": "CONTRIBUTING.md §3: 'all handlers must ...'",
      "confidence": "high|low",
      "posted_as": "inline|summary|suppressed",
      "comment_id": 1234567
    }
  ],
  "outcome": "changes_requested|escalated_to_human|skipped_ci_red",
  "ci_state": "success|failure|pending",
  "timestamp": "2026-08-26T12:00:00Z",
  "agent_version": "v1.0.0"
}
```

`confidence` lives per-finding rather than as a parallel array — same information as the PRD's `findings[] / confidence[]`, with no chance of the two drifting out of sync. The document is standalone and self-describing, so V2's task assigner can read it cold. Document the shape in `infra/firestore-schema.md` and mention it in the submission write-up: architectural discipline is 30% of the score, and a documented event contract is the cheapest evidence of it.

Index: composite on `repo` + `findings.path` for `fetch_past_reviews`. Create it on Day 3 when memory lands — Firestore will otherwise fail the query with a console link.

---

## 5. Day-by-day execution

### Day 1 — Tue Aug 26: prove the pipe

Definition of done: **a real PR opens and an ADK agent autonomously posts a comment on it.**

1. Install `gcloud`; create/select the project; enable Cloud Run, Pub/Sub, Firestore, Secret Manager, Vertex AI. Confirm the $150 credit is applied. (~45 min)
2. **GitHub App auth — hard 90-minute timebox.** App with PR read/write + checks read; private key into Secret Manager; JWT → installation token. At 90 minutes with no working token, stop and use the PAT fallback behind the same `get_token()` interface, then move on. This is the most common place this build dies.
3. Receiver: signature verification, `/healthz`, log-and-200. Deploy to Cloud Run, point a real webhook at it, open a test PR, watch the log. (~60 min)
4. Topic, DLQ, push subscription; receiver publishes, worker logs the envelope. (~60 min)
5. Worker posts a hardcoded comment on the PR. **The loop is closed here** — everything after this is quality. (~45 min)
6. Minimal ADK agent: one agent, one tool (`fetch_diff`), agent decides to call it and comments on what it read. (rest of day)

Also today: write `scripts/replay.py` and save a real webhook payload into `fixtures/` the first time one arrives. Every later iteration then runs against localhost in seconds instead of round-tripping through GitHub.

### Day 2 — Wed Aug 27: make it a real agent

- Split into three sub-agents; wire the full read/write tool set from §3.
- CI gate in the receiver, including the red-CI halt comment.
- Move from summary comment to **line-anchored inline comments**. This needs diff-position mapping and is the day's real work: GitHub rejects comments on lines outside the diff hunk, so build the position map from the patch and unit-test it against a saved diff.
- Idempotency check against Firestore before analyzing.

### Day 3 — Thu Aug 28: evidence and confidence gate

- `fetch_guidelines` (CONTRIBUTING.md + lint config), `fetch_file_context` for surrounding code.
- Confidence gate with the citation requirement enforced in the wrapper.
- Firestore memory (Tier 2) + `fetch_past_reviews`. **Most of the score gain lives here — protect it over polish elsewhere.**
- **Checkpoint, end of day: if Core (Day 1 + 2 scope) isn't fully working, stop building and spend Days 4–5 on packaging and demo.** Tier 3 (the override feedback loop) is already cut unless everything above is done and rehearsed.

### Day 4 — Fri Aug 29: harden and package

- All secrets in Secret Manager; confirm nothing sensitive sits in env vars or logs.
- DLQ verified: force a worker failure, watch the message land in `pr-review-dlq`.
- **Clean redeploy from a fresh clone following only the README** — this is what catches the undocumented step that would otherwise sink reproducibility.
- Architecture diagram + README.
- Build the demo PR: one real, findable issue of each of the three finding types, in a repo whose `CONTRIBUTING.md` makes at least one of them citable. Rehearse end to end.

### Day 5 — Sat Aug 30: record and write

- ~4-minute demo: 20–30s founder framing, then live PR → Cloud Run logs → Pub/Sub → GitHub comments appearing. Show the artifacts, not the code.
- Written description: features, stack, data sources, learnings. **Name the V2 loop as roadmap, explicitly not as built.**
- Confirm the Individual/Hobbyist category on Devpost; social post with `#AllThingsAgenticHackathon`.

### Sun Aug 31 — submit in the morning, not at 4:55pm.

---

## 6. Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| GitHub App auth eats Day 1 | High | Hard 90-min timebox → PAT + polling behind `get_token()` |
| Inline comment position mapping fails | High | Unit-test the patch→position map on Day 2; fall back to summary comment on rejection |
| ADK / Gemini model ID or SDK churn | Medium | Pin the model ID and every dependency version on Day 1; `pip freeze > requirements.txt` |
| Pub/Sub redelivery duplicates comments | Medium | Firestore idempotency key `{repo}:{pr}:{sha}`, checked before analysis |
| Agent produces vague, uncitable findings | Medium | Citation enforced in the tool wrapper, not only in the prompt |
| Cold start makes the demo look slow | Low | `--min-instances=1` on both services, demo day only |
| Python 3.14 default breaks the ADK install | Low | Pin the venv to 3.12 on Day 1, before anything else |

---

## 7. Definition of done for V1

- [ ] Opening a PR on the target repo produces inline comments and a "changes requested" review, with no human in the loop.
- [ ] Red CI produces a halt comment and no review.
- [ ] Every inline comment carries a citation; uncitable observations appear once, in the summary, as questions.
- [ ] The review is persisted to Firestore in the documented event shape.
- [ ] A redelivered webhook produces no duplicate comments.
- [ ] A fresh clone deploys from the README alone.
- [ ] Demo recorded, description written, submitted before noon on Aug 31.
