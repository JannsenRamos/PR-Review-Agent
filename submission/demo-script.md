# Demo script — ~4 minutes

Everything on screen is real and has already happened. No mockups, no staged
output. That is the point, and it is worth protecting: if a beat is not working
on the day, cut it rather than fake it.

---

## Before you hit record

**Tabs, in this order:**

1. `CONTRIBUTING.md` on GitHub — scrolled to the numbered rules
2. A fresh PR ready to open (a branch pushed, PR *not* yet created)
3. Cloud Run worker logs — filtered to `tool_call`
4. PR #2 — the reviewed one, comments visible
5. PR #3 — the red-CI halt
6. Firestore console — `reviews` collection, one document expanded to `decision`

**Terminal:** one window, log tail ready to run.

**Check first:** the fresh PR's branch has CI already finished, or the receiver
will defer instead of reviewing promptly.

---

## The beats

### 0:00–0:25 — The problem (you, to camera)

> "I'm a solo founder. I use AI to write code, so code comes out faster than one
> person can review it. There's no second engineer to catch the unhandled error
> path or the convention I drifted from. So I gave the mechanical half of code
> review to an agent, and kept the judgment half."

Do not mention the closed loop yet. Do not say "technical debt."

### 0:25–0:50 — The contract (tab 1)

Scroll the numbered rules. Read the file's own header aloud — it is the pitch in
the repo's words:

> "Rules are numbered so they can be cited. The agent quotes them verbatim and
> will not invent a convention that isn't written here."

> "Fifteen rules. This is the checklist, and it's the *only* thing the agent is
> allowed to assert against."

### 0:50–1:15 — Open the PR (tab 2)

Create the PR live. Say what you expect while it is thinking:

> "Nobody triggers this. A webhook fires, and everything after it is the agent's."

### 1:15–1:55 — It decides for itself (tab 3)

The `tool_call` lines, scrolling. This is the ADK criterion — do not rush it.

> "Every one of these is the model choosing a tool and calling it. Nothing in my
> application code calls a tool on its behalf — it decides what to fetch, in what
> order, and when it has enough."

### 1:55–2:35 — The comments, and the citation (tab 4)

Land on one inline comment. Point at the `> Cited:` line.

> "Every inline comment carries a verbatim quote from that file. And this is the
> part I actually care about: I never check whether the rule is real. It can't
> post a comment whose quote isn't in a document it fetched during this run —
> that check is in the tool wrapper, not the prompt. A prompt drifts. A wrapper
> doesn't."

Then scroll to the summary comment:

> "These two it couldn't cite. So instead of asserting them, it asked. Both are
> real bugs — one of them I didn't plant, it found on its own."

### 2:35–3:00 — What it refuses (tab 5, then back)

> "Red CI: it comments and stops. It never re-runs your tests."

> "And it has never approved anything, because there's no approve tool in the
> surface. That isn't a rule I told it to follow — it's a thing it cannot do.
> The worst case is that it wastes my time. Not that it waves something through."

### 3:00–3:30 — The record (tab 6)

Expand `decision`.

> "Every review is a document: what it found, how it classified it, what it
> suppressed and why, and — when it escalates — which of six reasons applied.
> 'Escalated to a human' on its own tells the next person nothing."

If time allows, the one statistic worth quoting:

> "Across eleven runs: twenty findings asserted inline, twelve demoted to
> questions. Over a third of what it noticed, it wasn't willing to claim."

### 3:30–3:45 — The honest trade

Say this before anyone asks it:

> "If you want the most findings, use one of the existing AI reviewers — they'll
> beat this on volume. This one is built for when you're going to act on the
> comment without double-checking it, because you're the only person who can."

### 3:45–4:00 — What's next (diagram, or the V2 PRD on screen)

> "This is the loop: ship, review, merge, watch what users do, decide what to
> build next. I built the review step. The next one is designed and written
> down — and deliberately not built. I'd rather show you one that works than two
> that half do."

---

## If you run long, cut in this order

1. The statistic at 3:00–3:30 (keep the `decision` document itself)
2. The summary-comment scroll at 2:25
3. The red-CI tab — mention it verbally instead of switching

**Never cut:** the citation close-up, or "there is no approve tool."

---

## Be ready for these

**"Doesn't Copilot already do this?"**
> "Yes, and it'll find more than mine does. The difference is mine can't make
> anything up. If it can't quote a rule from my repo, the comment doesn't post."

**"How do you know it's more precise?"**
Do not claim a measurement — there isn't one.
> "I don't have a benchmark. What I have is a guarantee: it's structurally
> incapable of citing a rule that isn't in the repo, and you can check that by
> reading forty lines of code."

**"What if the rules aren't written down?"**
> "Then it can't assert anything, and everything becomes a question in the
> summary. That's the designed behaviour, not a failure mode."

**"Can it be wrong?"**
Yes — say so, and name the sharpest case:
> "Test-gap findings quote a real rule, but the agent has no tool to check
> whether a test actually exists — so it's inferring. The gate proves the rule
> is real. It doesn't prove the claim is true. That's the limitation I'd fix
> first."
