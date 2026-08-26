# PRD — V1: Autonomous PR Review Agent

**Author:** [Founder]
**Status:** Draft v2 — hackathon-bound
**Deadline:** Aug 31, 2026, 5:00pm PDT — All Things Agentic Hackathon, Taskmaster track, Individual/Hobbyist category
**Companion doc:** See `prd-v2-task-assigner-loop.md` for the closed-loop roadmap this PRD is designed to support later. V2 is not in scope for this build.

---

## 1. Problem (shared with V2 — see companion doc)

**The founder's version.** As a solo founder using AI coding assistance, code output scales faster than review capacity. There is one person reading every diff, and that person is also doing sales, product, fundraising, and support. AI-generated code that looks plausible on a skim can carry unhandled error paths, convention drift, or untested new branches — a second engineer would normally catch this; solo, nothing does, until production does.

**The extended version, once shipped code turns into user feedback.** The same capacity gap exists one step downstream: feedback and usage signal pile up, and deciding what's actually worth building next is itself unreviewed, ad hoc, and easy to get wrong when it's the only person doing it, under time pressure, with sales/fundraising also demanding attention.

**The shape of the fix, end to end:** a closed loop — ship → review checkpoint → merge → observe real usage → decide what's worth building next → ship again — where each *mechanical* step is agent-owned and every *judgment* step stays human.

---


---

# PART A — V1: PR Review Agent

## 2. Scope

V1 solves the review checkpoint only. It does not write code, does not deploy, and does not decide what to build next — that's V2's job, and V1 is not blocked on it existing.

## 3. What it does

The agent owns the mechanical review pass end-to-end and escalates by design.

On PR open/update:
1. Reads CI check-run status. Red → reports and halts (reviewing broken code wastes compute).
2. Gathers evidence: diff, CONTRIBUTING.md/lint config, past review comments on the same files, surrounding code for changed functions.
3. Analyzes for three checkable classes:
   - Local defects (unhandled errors, null paths, resource leaks, injection risk)
   - Convention violations, cited against a stated rule or prior comment
   - New branches/error paths with no corresponding test
4. Gates every finding by confidence: high confidence + evidence → inline comment; low confidence → single summary comment, phrased as a question.
5. Acts on GitHub: inline comments, request-changes, reviewer assignment, labels.
6. Persists the review to per-repo memory (Firestore) for future retrieval.

## 4. Explicit non-goals

- **Never approves.** Terminal states: "changes requested" or "escalated to human."
- **Does not re-run tests** — reads CI results, doesn't reproduce them.
- **Does not commit code** — test gaps are suggested in comments, never auto-committed.
- **Does not judge whether code is right for the product** — only whether it's mechanically sound and consistent with stated conventions.

## 5. Architecture

```
GitHub PR event
      ↓
Cloud Run receiver   — validates signature, checks CI status, returns 200 in <1s
      ↓
Pub/Sub topic        — async job queue, retries, dead-letter on repeated failure
      ↓
Cloud Run worker      — ADK agent (3 sub-agents), Gemini 3.5+ via Vertex AI
      ↓ ↑
Firestore             — per-repo memory: reviews, comments, override records
      ↓
GitHub API            — inline comments, request changes, assign reviewer, label
```

**Forward-compatibility note (the only V2 concession in V1):** the Firestore schema for a completed review is written as a standalone, well-formed event document (`repo`, `pr_number`, `findings[]`, `confidence[]`, `outcome`, `timestamp`) rather than an internal-only shape. This makes it readable by a future consumer (V2's task assigner) without requiring V1 to know V2 exists. No queue, API, or plugin interface is built for this in V1 — just a clean, documented event shape.

### Agent structure (ADK)

| Agent | Responsibility | Tools |
|---|---|---|
| Diff analyzer | Parse change, identify affected paths and new branches | `fetch_diff`, `fetch_file_context` |
| Convention checker | Retrieve standards and past reviews, compare | `fetch_guidelines`, `fetch_past_reviews`, `fetch_ci_status` |
| Action executor | Apply confidence gate, act on GitHub | `post_inline_comment`, `request_changes`, `assign_reviewer`, `apply_label` |

## 6. Tech stack (mandatory requirements — all satisfied)

- **Gemini 3.5+** via Vertex AI, model version pinned explicitly
- **Google ADK** — multi-agent, native tool-calling; agent invokes tools itself, not application code on its behalf
- **Google Cloud services:** Cloud Run, Pub/Sub, Firestore, Secret Manager (four; one required)

GitHub App with private key in Secret Manager. Fallback: Personal Access Token + polling if App registration stalls.

## 7. Scope tiers

**Core (must ship by Day 4):** webhook → Pub/Sub → worker → ADK agent reads diff → posts inline comments, requests changes. CI gate included.

**Tier 2 (Day 4 if Core solid):** Firestore per-repo memory — retrieval of past comments as evidence context. Most of the score gain lives here.

**Tier 3 (only if ahead):** feedback loop — record human overrides, adjust future reviews. First thing cut.

**Cut rule:** if Core isn't done by end of Day 3, stop building and move to demo/packaging on Days 4–5.

## 8. Prerequisites

- Google Cloud project with billing enabled, $150 hackathon credit claimed
- A GitHub repo you control, with a test branch/PR workflow to demo against
- CONTRIBUTING.md or equivalent conventions doc in the target repo (even a minimal one) — the convention checker needs something to cite against
- GitHub App registered with pull request read/write + checks read permissions (or PAT fallback)

## 9. Build plan — day by day

**Day 1 — prove the pipe.** Goal: a PR opens, an ADK agent autonomously posts a comment.
1. Cloud setup (~30 min): enable Cloud Run, Pub/Sub, Firestore, Secret Manager, Vertex AI.
2. GitHub App auth — hard 90-minute timebox, then fall back to PAT + polling.
3. Receiver returns 200 (~60 min): validate signature, log, nothing else.
4. Pub/Sub wired through (~60 min): receiver publishes, worker logs.
5. Worker posts a hardcoded comment (~45 min): closes the loop.
6. Minimal ADK agent (rest of day): one agent, one tool, agent decides to invoke it.

**Day 2 — make it a real agent.**
- Expand to three ADK agents with the full tool set.
- Implement CI gate.
- Move to line-anchored inline comments.

**Day 3 — evidence and confidence gate.**
- Retrieval: CONTRIBUTING.md, lint config, surrounding code.
- Confidence gate with citation requirement.
- Firestore memory (Tier 2), written in the forward-compatible event shape (A4).
- **Checkpoint: if Core isn't fully working, stop building and move to Days 4–5 packaging.**

**Day 4 — harden and package.**
- Secret Manager, dead-letter topic.
- Clean redeploy, confirm README works from zero.
- Architecture diagram, README.
- Rehearse demo PR with a real, findable issue in it.

**Day 5 — submit early.**
- Record ~4-min demo: 20–30s founder framing, then live PR → Cloud Run logs → Pub/Sub queue → GitHub comments.
- Written description: features, stack, data sources, learnings — **name the V2 loop here as roadmap, not as built.**
- Confirm Individual/Hobbyist category on Devpost.
- Social post with #AllThingsAgenticHackathon.
- Submit the morning, not at the 5:00pm PDT cutoff.

## 10. Judging alignment

| Criterion | Weight | How this scores |
|---|---|---|
| Innovation & Operational Utility | 40% | Real decisions with consequences (blocks merges, routes reviewers); solves a lived founder problem |
| Architectural Discipline & Tech Stack | 30% | Decoupled receiver/worker, async retries/dead-lettering, persistent memory, secrets management, confidence-gated failure handling |
| Demo & Production Readiness | 30% | Live PR produces visible GitHub artifacts on camera; reproducible from README |

---

