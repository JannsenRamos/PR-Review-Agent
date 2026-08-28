# PR Review Agent

An autonomous pull request reviewer that owns the mechanical review pass and
escalates by design. It reads CI, gathers evidence, comments inline where it can
cite a rule, and asks questions where it cannot.

**It never approves.** The only terminal states are *changes requested* and
*escalated to human*. There is no approve tool for the agent to call.

Built for the All Things Agentic hackathon (Taskmaster track) on Google ADK,
Gemini via Vertex AI, Cloud Run, Pub/Sub, Firestore and Secret Manager.

---

## How it works

```
GitHub PR event
      ↓
receiver (Cloud Run)   verify HMAC → filter → CI gate → publish, 200 in <1s
      ↓
Pub/Sub                async queue, 5 attempts, dead-letters to pr-review-dlq
      ↓
worker (Cloud Run)     ADK agent: diff analyzer → convention checker → action executor
      ↓ ↑
Firestore              per-repo memory, idempotency, review events
      ↓
GitHub API             inline comments, request changes, assign reviewer, label
```

The split is the design. The receiver does admission control and nothing slow —
no LLM call, no diff fetch — so GitHub always gets a fast 200. Everything with
real latency runs on the async side, where retries and dead-lettering exist.

**Red CI halts the review.** Reviewing broken code wastes the run, so the
receiver comments and stops rather than publishing a job.

**Every inline comment carries a citation** — a quoted rule from
`CONTRIBUTING.md` or the lint config, or a prior review comment on the same
file. Findings that cannot be cited go into one summary comment, phrased as
questions. That gate is enforced in the tool wrapper, not just the prompt.

## Setup from zero

### 1. Local environment

Python **3.12** (not 3.14 — `google-adk` does not support it yet):

```bash
py -3.12 -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
./.venv/Scripts/python.exe -m pytest tests/ -q
./.venv/Scripts/python.exe -m ruff check .
```

On macOS/Linux use `python3.12` and `.venv/bin/python`.

`requirements-dev.txt` pulls in both services plus `pytest` and `ruff`. The two
service requirements files are what the images install and contain no test
tooling. CI installs the same dev file, so a green local run means a green CI
run.

On Windows, clone somewhere short (`C:\src\...`). Some dependencies exceed the
260-character path limit when nested deeply, and pip fails with a long-path
error that does not name the real cause.

### 2. GitHub App

Create a GitHub App with these repository permissions, subscribed to the
**Pull request** event:

| Permission | Level | Why |
|---|---|---|
| Pull requests | Read and write | inline comments, request changes, reviewers |
| Issues | Read and write | summary comments and labels go through `/issues/{n}/...` |
| Contents | Read-only | `CONTRIBUTING.md`, lint config, file context |
| Checks | Read-only | the CI gate |
| Metadata | Read-only | mandatory, auto-selected |

Install it on the target repo. Keep the App ID, the generated private key
`.pem`, and the webhook secret. The installation ID arrives in every webhook
payload, so there is nothing to record for it.

A Personal Access Token works as a fallback: set `GITHUB_PAT` instead of
`GITHUB_APP_ID` + `GITHUB_PRIVATE_KEY`. `get_token()` picks the mode; nothing
else in the codebase cares which is in use.

### 3. Google Cloud

Install the [gcloud CLI](https://cloud.google.com/sdk/docs/install), then:

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project YOUR_PROJECT
```

Create the secrets:

```bash
printf %s "$WEBHOOK_SECRET" | gcloud secrets create github-webhook-secret --data-file=-
gcloud secrets create github-private-key --data-file=path/to/app-key.pem
```

Pin the model. There is no default anywhere in the codebase and `deploy.sh`
aborts without one, on purpose: a plausible-but-wrong id deploys cleanly and
fails at the first Vertex call, which is a far worse place to find out.

```bash
export GEMINI_MODEL=<exact-model-id>          # e.g. gemini-3.7-flash
```

Listing publisher models is **not** a reliable availability check — the list
happily returns models a given project cannot call. The only trustworthy probe
is invoking one:

```bash
curl -H "Authorization: Bearer $(gcloud auth print-access-token)"   -H "Content-Type: application/json"   "https://aiplatform.googleapis.com/v1/projects/$GCP_PROJECT/locations/global/publishers/google/models/$GEMINI_MODEL:generateContent"   -d '{"contents":[{"role":"user","parts":[{"text":"hi"}]}]}'
```

Note `locations/global`, not your Cloud Run region. Gemini 3.x is served from
the global endpoint and 404s on `us-central1`; `VERTEX_LOCATION` defaults to
`global` for this reason.

### 4. Deploy

```bash
export GCP_PROJECT=your-project REGION=us-central1 GITHUB_APP_ID=123456
export GEMINI_MODEL=<exact-model-id>          # required; the script exits without it
bash infra/deploy.sh
```

The script is idempotent — re-run it freely. It enables APIs, creates the topic,
the dead-letter topic and its retention subscription, and the push subscription;
builds both images through Cloud Build; deploys both services; and grants the
IAM the push path needs. It prints the receiver URL; point the GitHub App's
webhook at `<receiver-url>/webhook`.

Two things that are easy to get wrong and cost an afternoon each:

- The push request carries an OIDC token for the **push-auth service account**,
  so that account needs `run.invoker` on the worker — not the Pub/Sub service
  agent. Granting the wrong one gives an endless 403 loop on `/jobs` that never
  reaches the application.
- Google's frontend reserves `/healthz` on `*.run.app` and answers it with its
  own 404 before the request reaches the container. Both services use `/health`.

### 5. Give it something to cite

The target repo needs a `CONTRIBUTING.md`, and benefits from a lint config
(`.ruff.toml`, `pyproject.toml`, `.eslintrc.json`, `setup.cfg` or `.flake8`).
These are the only documents an inline comment may be cited against.

The citation requirement is enforced on content, not on wording: the quote must
actually appear in a document the agent fetched during that run. A rule the
model paraphrases into existence is rejected at the point of posting and falls
back to the summary comment. So a repo with no written conventions produces only
low-confidence observations — by construction, not by luck.

Numbered rules work best. They are unambiguous to quote and easy to read back in
a review comment.

## Local development

Run the receiver and replay a saved payload instead of round-tripping through
GitHub:

```bash
export GITHUB_WEBHOOK_SECRET=whatever GCP_PROJECT=your-project
cd receiver && ../.venv/Scripts/python.exe -m uvicorn main:app --port 8080
```

```bash
./.venv/Scripts/python.exe scripts/replay.py fixtures/pull_request.opened.json
```

Replace `fixtures/pull_request.opened.json` with a real captured delivery as
soon as you have one — see `fixtures/README.md`.

### Tests

```bash
./.venv/Scripts/python.exe -m pytest tests/ -q          # all
./.venv/Scripts/python.exe -m pytest tests/test_diff_positions.py::test_multiple_hunks_restart_numbering -q
```

`tests/test_diff_positions.py` covers the diff line mapper — the piece whose
failure mode is a silent 422 from GitHub and an agent that appears to do
nothing. No network or credentials needed.

## Layout

| Path | What lives there |
|---|---|
| `receiver/` | HMAC verification, event filter, CI gate, Pub/Sub publish |
| `worker/main.py` | Pub/Sub push endpoint, idempotency, retry semantics |
| `worker/agent/` | ADK agents and their instruction text (`prompts.py`) |
| `worker/tools/` | The agent's tools — read, write, memory, diff mapping |
| `infra/` | `deploy.sh` and the Firestore document contract |
| `PRD/` | Requirements and the day-by-day implementation plan |

`github_auth.py` is duplicated into both services on purpose: two images, no
shared package to build and version.

## Status

Deployed and running against real pull requests on this repository.

Observed live, not asserted:

| Behaviour | Evidence |
|---|---|
| Reviews a PR unattended | PR #2 — 6 inline comments and a requested-changes review, no human in the loop |
| Every inline comment is cited | 6 verbatim quotes of `CONTRIBUTING.md`, each checked against the fetched document |
| Uncitable findings become questions | 2 observations in one summary comment, phrased as questions |
| Red CI halts the review | PR #3 — halt comment, zero inline comments, nothing published to Pub/Sub |
| Redelivery does not double-comment | PR #2 reopened at the same head SHA — `already reviewed`, comment count unchanged |
| Never approves | 5 review documents, all `changes_requested` or `escalated_to_human` |

Each review is persisted to Firestore in the shape documented in
`infra/firestore-schema.md`, including a `decision` block recording how the
outcome was reached and why it escalated when it did.

**Known limitation.** Of the three declared finding classes, `test_gap` is not
reliably produced: the agent has no tool that can determine whether a test
covering a new branch exists, so it would have to guess at file paths. Local
defects and convention violations work. This is stated rather than papered over —
the taxonomy is in the PRD and the prompt, and the gap is real.

The test suite is offline by design and covers structure and guardrails — the
absence of an approve tool, the citation gate, diff-position mapping, escalation
reasons — not model behaviour. Anything needing a live model belongs in a demo
run.

## Roadmap

V2 closes the loop: ship → review → merge → observe usage → decide what to build
next. Not built. The only concession to it here is that the Firestore review
document is written as a standalone, documented event
(`infra/firestore-schema.md`) that a future consumer can read cold.
