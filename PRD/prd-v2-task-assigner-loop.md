# PRD — V2: Task Assigner Loop

**Author:** [Founder]
**Status:** Design only. **Nothing in this document is built.**
**Companion doc:** `prd-v1-pr-review-agent.md` — V1 is built, deployed and
running. This document exists because V1's PRD references it, and because the
shape of V2 determined one decision inside V1 (see §7).

---

## 1. Problem

V1 solved the review checkpoint: code goes out faster than one person can review
it, so the mechanical half of review became agent-owned.

The same capacity gap exists one step downstream, and it is worse because it is
less visible. Feedback arrives — support messages, bug reports, feature asks,
things said in passing — and deciding *what is actually worth building next* is
unreviewed, ad hoc, and easy to get wrong under time pressure with sales and
fundraising also demanding attention. Nothing drops loudly. It just quietly never
gets done, and the thing that does get built is whatever was mentioned most
recently or most loudly.

## 2. The loop

```
ship → review checkpoint → merge → observe real usage → decide what to build → ship
        └─────── V1 ──────┘                    └─────── V2 ───────┘
```

V1 owns one mechanical step. V2 owns the other. Every *judgment* step in between
stays human — that is the thesis, not a limitation.

## 3. What V2 does

On a piece of feedback arriving:

1. **Normalises it.** Source, author, timestamp, verbatim text.
2. **Classifies it.** Bug, feature request, confusion, or noise. Noise stops here.
3. **Deduplicates against existing work.** The same complaint arriving five times
   is one item weighted five, not five items.
4. **Gathers evidence for importance.** How often, from whom, against what the
   product claims to do — never an unsupported assertion that something matters.
5. **Files it** as a GitHub issue with a proposed classification and the evidence
   attached, linked back to the source.
6. **Surfaces it to the founder** in one place — Slack — with what it found and
   what it could not decide.

## 4. Explicit non-goals

Inherited from V1, for the same reasons:

- **Never decides priority.** It ranks by *stated evidence* and proposes an
  order. Choosing what to build is the judgment step and stays human. This is
  V2's version of "never approves" and must be enforced the same way — by the
  absence of a tool that sets priority, not by an instruction not to.
- **Never closes or merges anything.**
- **Never replies to the person who gave the feedback.** Drafting a reply for a
  human to send is in scope; sending it is not.
- **Never invents a theme.** A cluster is only a cluster if the agent can quote
  the items in it, exactly as V1 can only assert what it can quote.

## 5. Open questions — the hard part

These are unresolved, and pretending otherwise would be the mistake:

**Where is the system of record?** Slack is a view, not a store. Notifications
scroll away and cannot be queried. The durable record should be GitHub issues (or
a board), with Slack as the inbox that tells a human something arrived. V1's
Firestore event is the model: one self-describing document per decision.

**What counts as feedback, and where does it come from?** Support email, Discord,
in-app widget, app-store reviews, sales calls. Ingestion is most of the work and
changes completely per source. V1 had exactly one trigger — a GitHub webhook —
and that simplicity is most of why it shipped in three days. V2 should start with
**one** source and resist adding a second until the loop closes.

**Importance cited against what?** This is V1's citation problem in a harder
form. V1 could quote `CONTRIBUTING.md`, a document that already existed. There is
no equivalent for "this matters" — so either the founder writes down what makes
something important (the closest analogue to `CONTRIBUTING.md`, and the reason V1
insists the target repo have one), or the agent is limited to citing countable
facts: how many people asked, whether it blocks a paid user, whether it
contradicts a documented promise. Anything beyond that is the agent asserting
judgment it has no evidence for.

**Deduplication is a judgment call.** "Is this the same complaint?" has no
equivalent of V1's `{repo}:{pr}:{head_sha}` — that key is exact and this one is
semantic. Getting it wrong in one direction floods the backlog; in the other, it
silently merges two different problems into one. This is the single hardest piece
and should be prototyped before anything else is designed around it.

**What does the agent do when it cannot decide?** V1's answer is
`escalated_to_human` with a stated reason, and V2 needs the same terminal state
for the same reason: an escalation that says *why* asks something specific of the
person picking it up.

## 6. What V1 already proves about this

The pattern transfers, and that is the argument for building V2 this way rather
than as a fresh design:

| | V1 (built) | V2 (this document) |
|---|---|---|
| Trigger | GitHub webhook | one feedback source |
| Evidence | diff, `CONTRIBUTING.md`, past reviews | the item, past items, existing issues |
| Classification | defect / convention / test_gap | bug / feature / confusion / noise |
| Gate | quote a rule, or ask a question | cite countable facts, or escalate |
| Action | comment, request changes, label | file an issue, link it, label |
| Refuses to | approve | set priority |
| Record | Firestore review event | equivalent, self-describing |

V1's harder-won lessons apply directly. A guardrail must be enforced in the tool
wrapper, not the prompt. The persisted record must be built from what the agent
*did*, not from what it says it did. And verifying that evidence is real is not
the same as verifying that a conclusion follows from it — V1's test-gap findings
cite a genuine rule while making an unverified claim, and V2's importance ranking
is exposed to exactly that failure, in a domain where it is much harder to spot.

## 7. The one concession V1 already made

V1 writes each completed review to Firestore as a **standalone, self-describing
event**, documented in `infra/firestore-schema.md`, rather than an internal shape
convenient to V1. It carries the repo, the PR, the findings with their types and
citations, the outcome, and a `decision` block recording how that outcome was
reached.

That is the only thing V1 built for V2's benefit. No queue, no API, no plugin
interface — a documented event a future consumer can read cold. It answers "what
shipped, and what was wrong with it", which is one input to "what should we build
next".

It is worth being clear about the limit: those documents describe **code review**,
not **user feedback**. V2's primary input does not exist yet and is not derivable
from anything V1 stores.

## 8. Scope, if this is ever built

**Tier 1** — one source, classify and file. No ranking, no clustering. A human
reads every item; the agent just makes sure nothing is lost and everything lands
in one place with its evidence attached.

**Tier 2** — deduplication and clustering, with the cluster quoting its members.

**Tier 3** — evidence-cited importance ranking, proposed and never applied.

Tier 1 is the one worth building first, and it is useful alone. That ordering is
the same reason V1 shipped: the smallest version that closes a loop beats the
complete version that does not.
