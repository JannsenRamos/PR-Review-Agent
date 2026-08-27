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
    GEMINI_MODEL,
    GCP_PROJECT,
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

    def record(self, **finding) -> None:
        self.findings.append(finding)

    @property
    def posted_inline(self) -> int:
        return sum(1 for f in self.findings if f.get("posted_as") == "inline")

    @property
    def outcome(self) -> str:
        return OUTCOME_CHANGES_REQUESTED if self.posted_inline else OUTCOME_ESCALATED


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
        return github_read.fetch_guidelines(repo, head_sha, installation_id)

    def fetch_past_reviews(paths: list[str]) -> dict:
        """Fetch previous review findings on these files in this repo.

        A prior comment on the same file is a valid citation: it shows the point
        has been raised before and was not a one-off opinion.
        """
        return _fetch_past_reviews(repo, paths, PAST_REVIEW_LIMIT)

    def fetch_ci_status() -> dict:
        """Read the CI check results for this commit. Never re-runs anything."""
        return github_read.fetch_ci_status(repo, head_sha, installation_id)

    # --- phase 3: act -------------------------------------------------------
    def post_inline_comment(path: str, line: int, body: str, citation: str) -> dict:
        """Post one comment anchored to a specific line, for a HIGH-confidence finding.

        Requires a citation: a verbatim quote from CONTRIBUTING.md or the lint
        config, or a prior review comment on this file. A finding you believe but
        cannot cite is not a high-confidence finding — put it in the summary
        comment instead.

        The line must be one this pull request added or touched. On any error,
        move the finding to the summary comment; do not retry the same call.
        """
        allowed = ledger.commentable.get(path)
        if allowed is not None and line not in allowed:
            ledger.record(
                type="unknown", path=path, line=line, summary=body,
                citation=citation, confidence="high", posted_as="suppressed",
                suppressed_reason="line_not_in_diff",
            )
            return {
                "error": "line_not_in_diff",
                "detail": f"Line {line} of {path} is not part of this diff. Use the summary comment.",
            }

        result = github_write.post_inline_comment(
            repo, pr_number, head_sha, path, line, body, citation, installation_id
        )
        if "error" in result:
            ledger.record(
                type="unknown", path=path, line=line, summary=body,
                citation=citation, confidence="high", posted_as="suppressed",
                suppressed_reason=result["error"],
            )
            return result

        ledger.record(
            type="unknown", path=path, line=line, summary=body,
            citation=citation, confidence="high", posted_as="inline",
            comment_id=result["comment_id"],
        )
        return result

    def post_summary_comment(body: str) -> dict:
        """Post the single summary comment holding every LOW-confidence observation.

        Phrase each observation as a question. Call this at most once.
        """
        result = github_write.post_summary_comment(repo, pr_number, body, installation_id)
        ledger.record(
            type="unknown", path=None, line=None, summary=body,
            citation=None, confidence="low", posted_as="summary",
            comment_id=result.get("comment_id"),
        )
        return result

    def request_changes(summary: str) -> dict:
        """Submit the review requesting changes. Use when you posted inline comments."""
        return github_write.request_changes(repo, pr_number, summary, installation_id)

    def assign_reviewer(logins: list[str]) -> dict:
        """Escalate to a human reviewer. Use when you could not review safely."""
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
    finally:
        await runner.close()

    event = write_review_event(
        repo, pr_number, head_sha, ledger.findings, ledger.outcome, ci_state
    )
    log.info(
        "review complete %s#%s outcome=%s inline=%d doc=%s",
        repo, pr_number, ledger.outcome, ledger.posted_inline, event.get("doc_id"),
    )
    return {"outcome": ledger.outcome, "findings": ledger.findings, **event}
