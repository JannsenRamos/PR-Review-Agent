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
./.venv/Scripts/python.exe -m pip install -r receiver/requirements.txt -r worker/requirements.txt
./.venv/Scripts/python.exe -m pytest tests/ -q
```

On macOS/Linux use `python3.12` and `.venv/bin/python`.

### 2. GitHub App

Create a GitHub App with **Pull requests: read & write**, **Checks: read**,
**Contents: read**, subscribed to the **Pull request** event. Install it on the
target repo. Keep the App ID, the generated private key `.pem`, and the webhook
secret.

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

Pin the model. Find the exact Gemini id available in your region and set it —
the code deliberately has no default:

```bash
gcloud ai models list --region=us-central1
export GEMINI_MODEL=<exact-model-id>
```

### 4. Deploy

```bash
export GCP_PROJECT=your-project REGION=us-central1 GITHUB_APP_ID=123456
bash infra/deploy.sh
```

The script is idempotent — re-run it freely. It enables APIs, creates the topic,
dead-letter topic and push subscription, grants the Pub/Sub service agent the
roles it needs, and deploys both services. It prints the receiver URL; point the
GitHub App's webhook at `<receiver-url>/webhook`.

### 5. Give it something to cite

The target repo needs a `CONTRIBUTING.md`. The convention checker quotes it
verbatim and will not invent a rule that is not written down, so a repo without
one produces only low-confidence observations.

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

Pipeline, tools and prompts are scaffolded; the ADK agent wiring in
`worker/agent/root.py` is the remaining piece. Until it exists, the worker posts
a placeholder comment so the plumbing can be proven end to end without the model
— see `run_review()` in `worker/main.py`.

## Roadmap

V2 closes the loop: ship → review → merge → observe usage → decide what to build
next. Not built. The only concession to it here is that the Firestore review
document is written as a standalone, documented event
(`infra/firestore-schema.md`) that a future consumer can read cold.
