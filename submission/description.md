# PR Review Agent

**All Things Agentic — Taskmaster track — Individual/Hobbyist**

An autonomous pull request reviewer that owns the mechanical review pass and
escalates by design. It reads CI, gathers evidence, comments inline where it can
cite a rule, and asks questions where it cannot.

**It never approves.** The only terminal states are *changes requested* and
*escalated to human*. There is no approve tool for the agent to call.

---

## The problem

As a solo founder using AI coding assistance, code output scales faster than
review capacity. One person reads every diff, and that person is also doing
sales, product, fundraising and support. AI-generated code that looks plausible
on a skim carries unhandled error paths, convention drift and untested branches.
A second engineer would catch this. Solo, nothing does — until production does.

This agent takes the mechanical half of that job: the part that is checkable
against a written rule. It deliberately does not take the judgment half.

## What it does

On a pull request opening or updating:

1. **Reads CI.** Red halts the review with a comment — reviewing broken code
   wastes the run. It never re-runs anything.
2. **Gathers evidence.** The diff, `CONTRIBUTING.md`, the lint config,
   surrounding source for changed regions, and prior review comments on the same
   files from its own memory.
3. **Finds three classes of issue**: local defects, convention violations, and
   new branches without tests. Product-fit judgments are explicitly out of scope.
4. **Gates on confidence.** A finding with a concrete file, line and a citation
   becomes an inline comment. Everything else goes into one summary comment,
   phrased as questions.
5. **Acts on GitHub**: inline comments, request changes, assign a reviewer, label.
6. **Persists the review** to Firestore as a self-describing event.

## It actually runs

Everything below was observed on real pull requests against this repository, not
asserted from the code:

| Behaviour | Evidence |
|---|---|
| Reviews unattended | PR #2 — inline comments and a requested-changes review, no human in the loop |
| Re-reviews against the new commit | Pushing the requested fix removed that finding and left the others |
| Labels the outcome | `agent-reviewed` + `changes-requested`, applied by the agent |
| Every inline comment is cited | Verbatim quotes of `CONTRIBUTING.md`, each verified against the fetched document |
| Uncitable findings become questions | 2 observations in a single summary comment |
| Red CI halts the review | PR #3 — halt comment, zero inline comments, nothing published to the queue |
| Redelivery does not double-comment | PR #2 reopened at the same head SHA — deduped, comment count unchanged |
| Never approves | 10 persisted reviews — 8 `changes_requested`, 2 `escalated_to_human`, 0 approved |
| Retrieves its own past reviews | `evidence_sources: 18` — prior findings on the same file pulled from Firestore into the evidence pool |
| Escalates with a stated reason | A run that legitimately found nothing recorded `escalation_reason: "found nothing in scope"` |

The agent also found a defect on PR #2 that was not planted: the code called
`response.json()` on a Slack webhook reply, which returns plain text. It raised
that as a question rather than an assertion, because no written rule covered it —
which is exactly what the confidence gate is for.

Memory is retrieved and used as evidence; every inline citation so far still
quotes `CONTRIBUTING.md`, because a written rule outranks a prior comment
wherever one exists. Worth stating precisely rather than implying more.

## Stack

- **Google ADK** — three sub-agents in sequence: diff analyzer, convention
  checker, action executor. Each owns its tools and calls them itself. No
  application code invokes a tool on the agent's behalf.
- **Gemini 3.7 Flash via Vertex AI**, model id pinned explicitly with no default
  anywhere in the codebase.
- **Cloud Run** ×2 — a receiver and a worker, deliberately split.
- **Pub/Sub** — async job queue, 5 delivery attempts, dead-lettering with a
  retention subscription so a poisoned job can be inspected.
- **Firestore** — per-repo memory, idempotency, and the persisted review event.
- **Secret Manager** — the GitHub App private key and webhook secret, mounted as
  env vars so neither service contains Secret Manager client code.
- **Cloud Build + Artifact Registry** — two images from one config.

### Why two services

The receiver does signature verification, event filtering and the CI gate, then
returns 200 in under a second with no LLM call and no diff fetch. The worker runs
the agent behind a Pub/Sub push subscription. All latency, retries and
dead-lettering live on the async side, where they belong. GitHub always gets a
fast 200; a model failure never becomes a webhook timeout.

## Data sources

GitHub REST API (diff, file contents, check runs, review comments), the target
repository's own `CONTRIBUTING.md` and lint config, and Firestore for prior
reviews of the same files.

## What I would point a reviewer at

**Guardrails are structural, not prompted.** "Never approves" is enforced by the
absence of an approve tool, not by an instruction. The citation requirement lives
in the tool wrapper, not the prompt — and it checks *provenance*, not length: a
quote must actually appear in a document fetched during that run, so a rule the
model paraphrases into existence is rejected at the point of posting and falls
back to the summary comment. Prompts drift under load; wrappers do not.

**The record is built from actions, not from the model's summary.** A ledger
accumulates real tool calls, and that is what gets persisted — a model that says
it commented and a model that commented are different things.

**Every review says how it decided.** The event carries a `decision` block:
counts by disposition and finding type, why each suppressed finding was dropped,
how many evidence sources were gathered, and — when it escalated — which of six
distinct reasons applied. A bare "escalated to human" tells the next person
nothing; "no conventions to cite" and "diff truncated" ask different things of
them.

## Learnings

**A guardrail that looks enforced can be enforcing nothing.** The citation gate
originally checked that a citation was at least twelve characters long. Twelve
plausible characters pass. An invented rule reads exactly like a real one, so the
check made the invariant *look* enforced while testing nothing about it. Fixing
it meant matching against the documents actually fetched.

**Then the fix failed in the opposite direction, and only a live run showed it.**
The new check pulled the quoted span out of a citation and treated backticks as
quote marks — so a rule containing inline code was split at every code boundary,
leaving a fragment too short to match, and a word-for-word correct citation was
rejected. Eight of fifteen rules in the target repo contain backticks. The agent
recovered by retrying with a shorter quote and the comment posted anyway, which
is precisely how it would have survived to demo day unnoticed.

**Scope guards protect the wrong case by default.** The check for "is this line
in the diff" read `if allowed is not None and line not in allowed` — which covers
files already known to be in the diff and waves through a file the PR never
touched. The dangerous input was the one that skipped the guard.

**Infrastructure lies quietly.** A dead-letter topic with no subscription
discards messages on arrival; dead-lettering appeared configured and retained
nothing. `gcloud run deploy --source` cannot build a two-service repo. Google's
frontend reserves `/healthz` on `*.run.app`. Pub/Sub push authenticates as the
push-auth service account, not the Pub/Sub service agent — getting that wrong
produces a 403 loop that never reaches the application and looks like an app bug.

## Known limitation

All three finding classes are produced — test-gap findings appear in live
reviews, cited against the target repo's written rule that a new branch needs a
test. But that class is weaker than it looks, and the weakness is worth naming
precisely.

The agent has no tool that can check whether a test exists. So when it reports a
test gap it is inferring absence from the diff, asserting "no test covers this"
without having looked. The citation gate does not catch this, and cannot: the
gate proves the quoted *rule* is real, not that the *claim* about the repository
is true.

A test-gap comment therefore cites a genuine rule and may still be wrong about
the facts. Local defects and convention violations do not share the problem —
both are judged entirely from text the agent fetched during the run. The fix is a
narrow `file_exists` tool bound to the diff analyzer; it is scoped and not built.

This is the sharpest thing I learned about the citation gate: verifying that
evidence is real is not the same as verifying that a conclusion follows from it.

## Roadmap — explicitly not built

V1 solves the review checkpoint only. The larger idea is a closed loop — ship →
review → merge → observe real usage → decide what is worth building next — where
every *mechanical* step is agent-owned and every *judgment* step stays human.

**None of that loop exists here.** The single concession to it is that the
Firestore review document is written as a standalone, documented event
(`infra/firestore-schema.md`) that a future consumer could read cold. No queue,
API or plugin interface was built for it.

The design is written down rather than hand-waved — `PRD/prd-v2-task-assigner-loop.md`
covers the trigger, the classification, the non-goals it inherits from V1, and
the four questions that are genuinely unresolved: where the system of record
lives, which feedback source to start with, what importance can honestly be cited
against, and how to deduplicate a complaint semantically when V1's exact
idempotency key has no equivalent. It also states the limit of what V1 hands
forward: the review documents describe code review, not user feedback, so V2's
primary input does not exist yet.
