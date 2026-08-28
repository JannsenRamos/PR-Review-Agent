"""ADK wiring: three sub-agents in sequence, each calling its own tools.

The agent invokes tools itself — no application code calls a tool on its behalf.
That is the whole point of the structure, so resist the temptation to "help" by
pre-fetching the diff and handing it over in the prompt.

Tools are bound as closures over the PR context, so the model supplies only the
arguments it actually has an opinion about (path, line, body, citation) and
cannot get the repo or commit SHA wrong.
"""

from __future__ import annotations

import logging
import os
import uuid

from config import (
    FINDING_TYPES,
    GCP_PROJECT,
    GEMINI_MODEL,
    OUTCOME_CHANGES_REQUESTED,
    OUTCOME_ESCALATED,
    PAST_REVIEW_LIMIT,
    VERTEX_LOCATION,
)
from tools import github_read, github_write
from tools.diff_positions import added_lines, commentable_lines
from tools.memory import fetch_past_reviews as _fetch_past_reviews
from tools.memory import write_review_event

from . import prompts

log = logging.getLogger("worker.agent")

APP_NAME = "pr-review-agent"


def _configure_vertex() -> None:
    """Point google-genai at Vertex rather than the public Gemini API."""
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
    if GCP_PROJECT:
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", GCP_PROJECT)
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", VERTEX_LOCATION)


class TransientModelError(RuntimeError):
    """Vertex was out of capacity. Retry the job; do not escalate the review."""


def _is_capacity_error(exc: Exception) -> bool:
    """True for 429/RESOURCE_EXHAUSTED, however the SDK happens to wrap it.

    Matched on the message rather than the exception class because ADK rewraps
    the genai error in its own private type, which is not a stable import.
    """
    import re

    # Vertex says "RESOURCE_EXHAUSTED", the SDK says "Resource exhausted." and
    # ADK's wrapper says "_ResourceExhaustedError" — strip everything that is
    # not alphanumeric so all three collapse to the same needle.
    text = re.sub(r"[^A-Z0-9]", "", f"{type(exc).__name__} {exc}".upper())
    return "429" in text or "RESOURCEEXHAUST" in text


class ReviewLedger:
    """Records what the agent actually did, for the Firestore event.

    Built from real tool calls rather than from the model's closing summary — a
    model that says it commented and a model that commented are different things,
    and only one of them is worth persisting.
    """

    def __init__(self) -> None:
        self.findings: list[dict] = []
        self.commentable: dict[str, set[int]] = {}
        self.added: dict[str, set[int]] = {}
        # Everything the agent actually fetched that a citation may quote. A
        # citation is only checkable against text we know it read.
        self.evidence: list[str] = []
        self.escalation_reason: str | None = None

    def record(self, **finding) -> None:
        self.findings.append(finding)

    def add_evidence(self, *texts: str | None) -> None:
        self.evidence.extend(t for t in texts if t)

    @property
    def evidence_text(self) -> str:
        return "\n".join(self.evidence)

    @property
    def posted_inline(self) -> int:
        return sum(1 for f in self.findings if f.get("posted_as") == "inline")

    @property
    def outcome(self) -> str:
        return OUTCOME_CHANGES_REQUESTED if self.posted_inline else OUTCOME_ESCALATED

    def _counts(self, key: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.findings:
            value = f.get(key)
            if value:
                counts[value] = counts.get(value, 0) + 1
        return counts

    @property
    def why_escalated(self) -> str | None:
        """Why no inline comment was posted, in one machine-readable token.

        Four different situations previously collapsed into the same bare
        "escalated_to_human" with nothing to distinguish them: a diff too large
        to read, a repo with no written conventions, a model that found nothing,
        and findings that were all rejected by the gate. They call for different
        responses from the human picking it up, so they are recorded apart.
        """
        if self.posted_inline:
            return None
        if self.escalation_reason:
            return self.escalation_reason
        if not self.findings:
            return "no_findings"
        if not any(f.get("posted_as") == "summary" for f in self.findings):
            return "all_findings_suppressed"
        if not self.evidence:
            return "no_conventions_to_cite"
        return "no_citable_findings"

    @property
    def decision(self) -> dict:
        """The audit trail for how this review landed where it did.

        Persisted and logged, so "why did it escalate" is answerable from the
        record rather than by re-reading the model's prose.
        """
        return {
            "outcome": self.outcome,
            "posted_as": self._counts("posted_as"),
            "by_type": self._counts("type"),
            "suppressed_reasons": self._counts("suppressed_reason"),
            "evidence_sources": len(self.evidence),
            "escalation_reason": self.why_escalated,
        }


def _bind_tools(repo: str, pr_number: int, head_sha: str, installation_id, ledger: ReviewLedger):
    """Build the three per-phase tool sets for one PR."""

    # --- phase 1: read the change ------------------------------------------
    def fetch_diff() -> dict:
        """Fetch the full diff of this pull request, file by file.

        Returns each changed file's path, status and unified-diff patch. Call
        this first. If the result has truncated=true the diff was too large to
        review honestly — say so and stop rather than skimming it.
        """
        result = github_read.fetch_diff(repo, pr_number, installation_id)
        for f in result.get("files", []):
            ledger.commentable[f["path"]] = commentable_lines(f.get("patch"))
            ledger.added[f["path"]] = added_lines(f.get("patch"))
        return result

    def fetch_file_context(path: str, start_line: int, end_line: int) -> dict:
        """Read a range of lines from a file as it exists in this pull request.

        Use this when the patch alone does not show enough to judge a change —
        for example to see whether an exception is handled further up the
        function, or whether a helper already validates its input.
        """
        return github_read.fetch_file_context(repo, path, head_sha, start_line, end_line, installation_id)

    # --- phase 2: gather evidence ------------------------------------------
    def fetch_guidelines() -> dict:
        """Fetch this repo's CONTRIBUTING.md and lint configuration.

        These are the only documents a convention finding may be cited against.
        A null result means no conventions are written down, which makes every
        convention finding low-confidence by definition.
        """
        result = github_read.fetch_guidelines(repo, head_sha, installation_id)
        ledger.add_evidence(result.get("contributing"), result.get("lint_config"))
        return result

    def fetch_past_reviews(paths: list[str]) -> dict:
        """Fetch previous review findings on these files in this repo.

        A prior comment on the same file is a valid citation: it shows the point
        has been raised before and was not a one-off opinion.
        """
        result = _fetch_past_reviews(repo, paths, PAST_REVIEW_LIMIT)
        for review in result.get("reviews", []):
            for finding in review.get("findings", []):
                ledger.add_evidence(finding.get("summary"), finding.get("citation"))
        return result

    def fetch_ci_status() -> dict:
        """Read the CI check results for this commit. Never re-runs anything."""
        return github_read.fetch_ci_status(repo, head_sha, installation_id)

    # --- phase 3: act -------------------------------------------------------
    def post_inline_comment(
        path: str, line: int, body: str, citation: str, finding_type: str = ""
    ) -> dict:
        """Post one comment anchored to a specific line, for a HIGH-confidence finding.

        finding_type must be one of "defect", "convention" or "test_gap" —
        nothing else is a finding.

        Requires a citation: a verbatim quote from CONTRIBUTING.md or the lint
        config, or a prior review comment on this file. Quote it word for word;
        the wording is checked against the documents you fetched, and a rule
        that is not in them will be rejected. A finding you believe but cannot
        quote is not a high-confidence finding — put it in the summary comment
        instead.

        The line must be one this pull request added or touched. On any error,
        move the finding to the summary comment; do not retry the same call.
        """
        def suppress(reason: str) -> None:
            ledger.record(
                type=kind, path=path, line=line, summary=body,
                citation=citation, confidence="high", posted_as="suppressed",
                suppressed_reason=reason,
            )

        # Defaulted rather than required so an omitted classification lands here,
        # as a recorded refusal the agent can act on. Left required, ADK raises
        # TypeError before this function runs and the attempt vanishes from the
        # ledger - which is how a comment aimed outside the diff went unrecorded
        # on the first live run.
        kind = finding_type if finding_type in FINDING_TYPES else "unknown"
        if kind == "unknown":
            suppress("unknown_finding_type")
            return {
                "error": "unknown_finding_type",
                "detail": f"finding_type must be one of {', '.join(FINDING_TYPES)}.",
            }

        # An unknown path is the dangerous case, not the safe one: .get()
        # returning None used to skip this guard entirely, so a comment on a file
        # the PR never touched sailed through to GitHub and was stopped only by a
        # 422 - a network round trip enforcing an invariant that belongs here.
        allowed = ledger.commentable.get(path)
        if allowed is None:
            suppress("file_not_in_diff")
            return {
                "error": "file_not_in_diff",
                "detail": (
                    f"{path} is not part of this pull request. Review only the files "
                    "fetch_diff returned."
                ),
            }
        if line not in allowed:
            suppress("line_not_in_diff")
            return {
                "error": "line_not_in_diff",
                "detail": f"Line {line} of {path} is not part of this diff. Use the summary comment.",
            }

        # The gate that makes the citation requirement real: the quote has to
        # appear in something this agent actually fetched. Checked here rather
        # than in the prompt, and on content rather than on length — a rule the
        # model invented reads exactly like a rule it read.
        if not github_write.citation_is_grounded(citation, ledger.evidence_text):
            suppress("citation_not_grounded")
            return {
                "error": "citation_not_grounded",
                "detail": (
                    "That wording is not in the guidelines or past reviews you fetched. "
                    "Quote a shorter span word for word, or move this to the summary "
                    "comment as a question."
                ),
            }

        result = github_write.post_inline_comment(
            repo, pr_number, head_sha, path, line, body, citation, installation_id
        )
        if "error" in result:
            suppress(result["error"])
            return result

        ledger.record(
            type=kind, path=path, line=line, summary=body,
            citation=citation, confidence="high", posted_as="inline",
            comment_id=result["comment_id"],
        )
        return result

    def post_summary_comment(body: str, observations: list[dict]) -> dict:
        """Post the single summary comment holding every LOW-confidence observation.

        Phrase each observation as a question. Call this at most once.

        observations records the same points in structured form, one entry per
        observation, each {"type", "path", "line", "summary"} — type being
        "defect", "convention" or "test_gap". This is what a human reviewer
        picking the PR up reads instead of re-parsing your prose, so it must
        match what the comment body says.
        """
        result = github_write.post_summary_comment(repo, pr_number, body, installation_id)
        comment_id = result.get("comment_id")

        # One ledger row per observation, not one per comment: the review event
        # documents findings, and "a summary was posted" is not a finding. Falls
        # back to a single untyped row if the model gave no structure.
        for obs in observations or [{"summary": body}]:
            kind = obs.get("type")
            ledger.record(
                type=kind if kind in FINDING_TYPES else "unknown",
                path=obs.get("path"),
                line=obs.get("line"),
                summary=obs.get("summary") or body,
                citation=None,
                confidence="low",
                posted_as="summary",
                comment_id=comment_id,
            )
        return result

    def request_changes(summary: str) -> dict:
        """Submit the review requesting changes. Use when you posted inline comments."""
        return github_write.request_changes(repo, pr_number, summary, installation_id)

    def assign_reviewer(logins: list[str], reason: str) -> dict:
        """Escalate to a human reviewer. Use when you could not review safely.

        reason is a short phrase saying what stopped you — "diff truncated",
        "no written conventions to cite", "found nothing in scope". It is stored
        on the review event, so the person picking this up knows what you were
        unsure about without reading the thread.
        """
        ledger.escalation_reason = (reason or "").strip()[:200] or "unspecified"
        return github_write.assign_reviewer(repo, pr_number, logins, installation_id)

    def apply_label(labels: list[str]) -> dict:
        """Apply labels to this pull request."""
        return github_write.apply_label(repo, pr_number, labels, installation_id)

    return (
        [fetch_diff, fetch_file_context],
        [fetch_guidelines, fetch_past_reviews, fetch_ci_status],
        [
            post_inline_comment,
            post_summary_comment,
            request_changes,
            assign_reviewer,
            apply_label,
        ],
    )


def _log_tool_call(tool=None, args=None, tool_context=None, **_ignored):
    """Make the agent's decisions visible in Cloud Run logs.

    Worth the few lines: on demo day this log is the evidence that the agent
    chose its own tool calls rather than following a script.

    Signature is keyword-tolerant on purpose — ADK invokes this callback with
    keyword arguments (tool=, args=, tool_context=), and a positional-only
    signature raises TypeError on the first tool call, mid-review.
    """
    name = getattr(tool, "name", tool)
    log.info("tool_call %s args=%s", name, {k: str(v)[:120] for k, v in (args or {}).items()})
    return None


def build_agent(repo: str, pr_number: int, head_sha: str, installation_id, ledger: ReviewLedger):
    from google.adk.agents import LlmAgent, SequentialAgent

    if not GEMINI_MODEL:
        raise RuntimeError(
            "GEMINI_MODEL is not set. Resolve the exact model id available in "
            f"{VERTEX_LOCATION} (gcloud ai model-garden models list | grep gemini) "
            "and set it in the environment. There is deliberately no default."
        )

    read_tools, evidence_tools, act_tools = _bind_tools(
        repo, pr_number, head_sha, installation_id, ledger
    )
    context_note = f"\n\nYou are reviewing {repo} PR #{pr_number} at commit {head_sha[:7]}."

    analyzer = LlmAgent(
        name="diff_analyzer",
        model=GEMINI_MODEL,
        description="Parses the change and identifies candidate findings.",
        instruction=prompts.DIFF_ANALYZER + context_note,
        tools=read_tools,
        output_key="diff_analysis",
        before_tool_callback=_log_tool_call,
    )

    checker = LlmAgent(
        name="convention_checker",
        model=GEMINI_MODEL,
        description="Attaches citable evidence to each candidate finding.",
        instruction=prompts.CONVENTION_CHECKER + context_note,
        tools=evidence_tools,
        output_key="evidence",
        before_tool_callback=_log_tool_call,
    )

    executor = LlmAgent(
        name="action_executor",
        model=GEMINI_MODEL,
        description="Applies the confidence gate and acts on GitHub.",
        instruction=prompts.ACTION_EXECUTOR + context_note,
        tools=act_tools,
        output_key="actions",
        before_tool_callback=_log_tool_call,
    )

    # Sequential, not a hierarchy with transfers: the phases are ordered and a
    # model that can jump back to analysis after acting is a model that can
    # comment twice.
    #
    # ADK 2.7 deprecates SequentialAgent in favour of Workflow. Not migrated on
    # purpose: Workflow is a graph API with a different model (nodes, triggers,
    # edges), SequentialAgent still works, and rebuilding the orchestration on a
    # five-day clock buys nothing a judge will see. Revisit after the deadline.
    return SequentialAgent(
        name="pr_review_agent",
        description=prompts.ROOT,
        sub_agents=[analyzer, checker, executor],
    )


async def review_pull_request(
    repo: str, pr_number: int, head_sha: str, installation_id, ci_state: str
) -> dict:
    """Run one review end to end and persist it. Returns the review event."""
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    _configure_vertex()
    ledger = ReviewLedger()
    agent = build_agent(repo, pr_number, head_sha, installation_id, ledger)

    runner = InMemoryRunner(agent=agent, app_name=APP_NAME)
    session_id = f"{repo}-{pr_number}-{head_sha[:7]}-{uuid.uuid4().hex[:8]}"
    await runner.session_service.create_session(
        app_name=APP_NAME, user_id="pr-review-agent", session_id=session_id
    )

    kickoff = types.Content(
        role="user",
        parts=[types.Part(text=f"Review {repo} pull request #{pr_number} at commit {head_sha}.")],
    )

    try:
        async for event in runner.run_async(
            user_id="pr-review-agent", session_id=session_id, new_message=kickoff
        ):
            if getattr(event, "author", None) and event.is_final_response():
                log.info("phase complete: %s", event.author)
    except Exception as exc:
        # A 429 is capacity, not a bad answer. Escalating it to a human would
        # burn the review for a condition that clears on its own, so hand it
        # back to Pub/Sub — but only while nothing has been posted yet, since a
        # redelivery re-runs every phase and would duplicate comments.
        if _is_capacity_error(exc) and not ledger.findings:
            raise TransientModelError(str(exc)[:200]) from exc
        log.exception("agent run failed after %d recorded action(s)", len(ledger.findings))
    finally:
        await runner.close()

    decision = ledger.decision
    event = write_review_event(
        repo, pr_number, head_sha, ledger.findings, ledger.outcome, ci_state, decision
    )
    # One line carrying the whole decision, so the demo can point at the log and
    # say why the agent did what it did rather than that it did something.
    log.info(
        "review complete %s#%s decision=%s doc=%s",
        repo, pr_number, decision, event.get("doc_id"),
    )
    return {
        "outcome": ledger.outcome,
        "findings": ledger.findings,
        "decision": decision,
        **event,
    }
