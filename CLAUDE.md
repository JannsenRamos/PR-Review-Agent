# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository state

Scaffolded, not yet deployed. Receiver, worker plumbing, agent tools and prompts exist; `worker/agent/root.py` (the ADK wiring) does not. Until it does, `run_review()` in `worker/main.py` posts a placeholder comment so the pipeline can be proven without the model. `main` has no commits yet.

Source of truth for what is being built:

- `PRD/prd-v1-pr-review-agent.md` — product requirements, scope tiers, non-goals
- `PRD/implementation-plan-v1.md` — repo layout, component specs, tool contracts, day-by-day plan

Read both before writing code. The plan fixes decisions (tool signatures, Firestore document shape, service split) that are expensive to change once anything builds against them.

## Deadline context

This is a hackathon build due **Aug 31, 2026, 5:00pm PDT** (All Things Agentic, Taskmaster track). The plan's Day-3 cut checkpoint is binding: if the Core pipeline isn't working by end of Thu Aug 28, building stops and the remaining time goes to demo and packaging. Prefer the shipping path over the elegant one, and say so when the two diverge.

## Architecture

Event-driven, two Cloud Run services, decoupled by Pub/Sub:

```
GitHub PR event → receiver (Cloud Run) → Pub/Sub → worker (Cloud Run) → GitHub API
                                                        ↕
                                                    Firestore
```

The split is the point, not an accident: the **receiver** does signature verification, event filtering, and the CI gate, then returns 200 in under a second with no LLM call and no diff fetch. The **worker** runs the ADK agent behind a Pub/Sub push subscription, so all latency, retries, and dead-lettering live on the async side.

The worker's agent is three ADK sub-agents in sequence — diff analyzer, convention checker, action executor — each owning its own tools. **The agent invokes its tools itself; application code must never call a tool on the agent's behalf.** That is a scored judging criterion, not a style preference.

### Invariants

These come from the PRD's non-goals and are enforced structurally, not by convention:

- **Never approves.** Terminal states are `changes_requested` or `escalated_to_human`. There is no approve tool in the tool surface — keep it that way rather than adding one and instructing the agent not to call it.
- **Every inline comment carries a citation** (a quoted rule from `CONTRIBUTING.md` / lint config, or a prior review comment on the same file). Enforced in the `post_inline_comment` wrapper, not only in the prompt — prompts drift, wrappers don't. Uncitable observations go into one summary comment, phrased as questions.
- **Reads CI, never re-runs it.** Red CI halts the review with a comment.
- **Never commits code.** Test gaps are suggested in comments only.
- **Findings are limited to three classes**: local defects, convention violations, and new branches/error paths without tests. Product-fit judgments are out of scope.

### Cross-cutting concerns

- **Idempotency key `{repo}:{pr_number}:{head_sha}`** is the Firestore document ID and the dedupe check. Pub/Sub is at-least-once; without checking this before analysis, a redelivery double-comments on the PR.
- **Tools return `{"error": ...}` rather than raising.** The agent must be able to see a failure and route around it — e.g. an inline comment rejected for falling outside the diff hunk falls back to the summary comment.
- **Transient failure → HTTP 500** from the worker so Pub/Sub redelivers. A model failure (no findings, unusable output) is *not* a 500; it is a completed review with `outcome: "escalated_to_human"`.
- **The Firestore `reviews` document is a public contract.** It is written as a standalone, self-describing event so a future V2 consumer can read it cold. Changing its shape is a breaking change — see `PRD/implementation-plan-v1.md` §4.

## Commands

Windows paths shown; on macOS/Linux use `.venv/bin/python`.

```bash
py -3.12 -m venv .venv                                  # 3.12 only — see below
./.venv/Scripts/python.exe -m pip install -r receiver/requirements.txt -r worker/requirements.txt
./.venv/Scripts/python.exe -m pytest tests/ -q          # all tests
./.venv/Scripts/python.exe -m pytest tests/test_diff_positions.py::test_multiple_hunks_restart_numbering -q
bash infra/deploy.sh                                    # idempotent; needs GCP_PROJECT, REGION
```

Local loop — run the receiver, replay a saved delivery rather than round-tripping through GitHub:

```bash
cd receiver && GITHUB_WEBHOOK_SECRET=x GCP_PROJECT=y ../.venv/Scripts/python.exe -m uvicorn main:app --port 8080
GITHUB_WEBHOOK_SECRET=x ./.venv/Scripts/python.exe scripts/replay.py fixtures/pull_request.opened.json
```

`replay.py` signs the payload, so the real signature path stays exercised locally.

## Toolchain constraints

Verified on this machine, Aug 26 2026:

- **Use Python 3.12** (`py -3.12`). The default `python` is 3.14.2; `google-adk` 2.7.1 installs and imports cleanly on 3.12, which is what `.venv` and both Dockerfiles use.
- **`gcloud` is not installed.** Nothing deploys until the Google Cloud CLI is installed and both `gcloud auth login` and `gcloud auth application-default login` have run.
- Available: node 24.13.0, gh 2.97.0, docker 29.1.3, git 2.42.0.
- **`GEMINI_MODEL` has no default on purpose.** Resolve the exact ID available in the project's Vertex AI region (`gcloud ai models list --region=...`) and set it in the environment. Never guess a model ID.
- Requirements are loosely bounded pending a first clean install; `pip freeze` into them once the stack is proven.

## Working practices

- Keep GitHub auth behind a single `get_token()` interface. The GitHub App path is primary; a PAT is the timeboxed fallback, and callers must not care which is in use.
- `scripts/replay.py` plus a saved webhook payload in `fixtures/` is the inner dev loop. Iterate against localhost rather than round-tripping through GitHub.
- Diff-position mapping (patch → GitHub comment position) is the highest-risk piece of code in the build. Unit-test it against a saved diff.
- The README is judged on reproducibility from zero. When a deploy step is discovered by hand, write it down immediately.
