"""Offline checks on the agent wiring.

No Vertex call and no GitHub call: these verify the structure and the guardrails,
which are the parts that must not quietly regress. Anything needing a real model
belongs in the live demo run, not here.

    py -3.12 -m pytest tests/ -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))

from agent import root  # noqa: E402
from tools import github_write  # noqa: E402

PR = ("owner/repo", 7, "abc1234def", None)


def _tools(ledger):
    return root._bind_tools(*PR, ledger)


def test_no_approve_tool_exists_anywhere():
    """'Never approves' is enforced by absence, so assert the absence."""
    _, _, act = _tools(root.ReviewLedger())
    names = {fn.__name__ for fn in act}
    assert not any("approve" in n for n in names)
    assert not any("approve" in n for n in dir(github_write))


def test_citation_is_required_before_any_network_call():
    # Empty citation must be rejected by the wrapper, not by GitHub. If this
    # regressed it would only show up as an uncited comment on a real PR.
    result = github_write.post_inline_comment(
        "owner/repo", 7, "abc1234", "a.py", 3, "This leaks a file handle", "", None
    )
    assert result["error"] == "citation_required"


def test_line_outside_the_diff_is_suppressed_not_posted():
    ledger = root.ReviewLedger()
    _, _, act = _tools(ledger)
    post_inline_comment = next(f for f in act if f.__name__ == "post_inline_comment")

    ledger.commentable["a.py"] = {10, 11, 12}
    ledger.add_evidence("Handle every error at the boundary.")
    result = post_inline_comment(
        "a.py", 99, "Unhandled error", "CONTRIBUTING.md: 'handle every error at the boundary'", "defect"
    )

    assert result["error"] == "line_not_in_diff"
    assert ledger.findings[0]["posted_as"] == "suppressed"
    assert ledger.posted_inline == 0


def test_outcome_is_escalation_when_nothing_was_posted_inline():
    ledger = root.ReviewLedger()
    assert ledger.outcome == "escalated_to_human"
    ledger.record(path="a.py", line=1, posted_as="inline", confidence="high")
    assert ledger.outcome == "changes_requested"


def test_build_agent_refuses_an_unpinned_model(monkeypatch):
    monkeypatch.setattr(root, "GEMINI_MODEL", "")
    with pytest.raises(RuntimeError, match="GEMINI_MODEL"):
        root.build_agent(*PR, root.ReviewLedger())


def test_agent_structure_and_tool_surface(monkeypatch):
    monkeypatch.setattr(root, "GEMINI_MODEL", "gemini-test-model")
    agent = root.build_agent(*PR, root.ReviewLedger())

    assert [a.name for a in agent.sub_agents] == [
        "diff_analyzer",
        "convention_checker",
        "action_executor",
    ]

    surface = {a.name: {t.__name__ for t in a.tools} for a in agent.sub_agents}
    assert surface["diff_analyzer"] == {"fetch_diff", "fetch_file_context"}
    assert surface["convention_checker"] == {
        "fetch_guidelines",
        "fetch_past_reviews",
        "fetch_ci_status",
    }
    assert "post_inline_comment" in surface["action_executor"]
    assert "request_changes" in surface["action_executor"]


def test_adk_can_derive_a_schema_for_every_tool(monkeypatch):
    """Catches a tool whose signature or docstring ADK cannot turn into a
    declaration — which would otherwise surface as a runtime error mid-review."""
    from google.adk.tools import FunctionTool

    monkeypatch.setattr(root, "GEMINI_MODEL", "gemini-test-model")
    agent = root.build_agent(*PR, root.ReviewLedger())

    for sub in agent.sub_agents:
        for fn in sub.tools:
            declaration = FunctionTool(func=fn)._get_declaration()
            assert declaration.name == fn.__name__
            assert declaration.description, f"{fn.__name__} needs a docstring"


def test_review_key_has_no_slash():
    """Firestore reads '/' as a path separator, so an unescaped "owner/name"
    raises 'A document must have an even number of path elements' at write time —
    caught in production, not by the earlier offline tests."""
    from config import review_key

    key = review_key("JannsenRamos/PR-Review-Agent", 1, "04f3005")
    assert "/" not in key
    assert key == "JannsenRamos_PR-Review-Agent:1:04f3005"


def test_capacity_errors_are_distinguished_from_bad_answers():
    """A 429 is transient capacity, not an unusable model answer: it must be
    retried by Pub/Sub, not escalated to a human."""
    assert root._is_capacity_error(RuntimeError("429 RESOURCE_EXHAUSTED"))
    assert root._is_capacity_error(RuntimeError("Resource exhausted. Try later."))
    assert not root._is_capacity_error(RuntimeError("404 NOT_FOUND"))
    assert not root._is_capacity_error(ValueError("bad json from model"))


# --- the citation gate: what it actually enforces ---------------------------


def test_a_citation_must_quote_something_the_agent_fetched():
    """The gate the length check could not make: an invented rule reads exactly
    like a real one, so the wording is matched against the fetched documents."""
    ledger = root.ReviewLedger()
    _, _, act = _tools(ledger)
    post_inline_comment = next(f for f in act if f.__name__ == "post_inline_comment")

    ledger.commentable["a.py"] = {10}
    ledger.add_evidence("Every outbound call must check the status before parsing the body.")

    result = post_inline_comment(
        "a.py", 10, "Response parsed without a status check",
        "CONTRIBUTING.md: 'all responses must be validated against a schema'", "convention",
    )

    assert result["error"] == "citation_not_grounded"
    assert ledger.posted_inline == 0
    assert ledger.findings[0]["suppressed_reason"] == "citation_not_grounded"


def test_a_verbatim_quote_survives_rewrapping_and_framing():
    """A real quote must still pass after the model wraps it across lines and
    prefixes the filename, or the gate would reject honest citations."""
    evidence = "Every outbound call must check the status\nbefore parsing the body."
    citation = "CONTRIBUTING.md: 'every outbound call must check the status before parsing the body'"
    assert github_write.citation_is_grounded(citation, evidence)


def test_nothing_can_be_cited_when_no_evidence_was_gathered():
    """A repo with no written conventions makes every finding low-confidence by
    definition — the gate has to say so rather than trusting the wording."""
    assert not github_write.citation_is_grounded("CONTRIBUTING.md: 'some plausible rule'", "")


def test_findings_are_recorded_with_their_class_not_as_unknown():
    """The Firestore contract types every finding; the ledger used to stamp them
    all "unknown", so the taxonomy existed only in the prompt."""
    ledger = root.ReviewLedger()
    _, _, act = _tools(ledger)
    post_inline_comment = next(f for f in act if f.__name__ == "post_inline_comment")

    ledger.commentable["a.py"] = {10}
    ledger.add_evidence("Close every file handle you open.")
    result = post_inline_comment(
        "a.py", 10, "Leaks a handle", "CONTRIBUTING.md: 'close every file handle you open'", "not_a_real_class"
    )

    assert result["error"] == "unknown_finding_type"
    assert ledger.findings[0]["suppressed_reason"] == "unknown_finding_type"


def test_summary_observations_become_typed_findings():
    """One row per observation, not one row per comment: a summary comment is
    not itself a finding."""
    ledger = root.ReviewLedger()
    _, _, act = _tools(ledger)
    post_summary_comment = next(f for f in act if f.__name__ == "post_summary_comment")

    posted = {}
    github_write_post = github_write.post_summary_comment
    try:
        github_write.post_summary_comment = lambda *a, **k: posted.setdefault("r", {"comment_id": 99})
        post_summary_comment(
            "Two questions about this change.",
            [
                {"type": "test_gap", "path": "a.py", "line": 4, "summary": "New branch has no test?"},
                {"type": "defect", "path": "b.py", "line": 9, "summary": "Timeout intentional?"},
            ],
        )
    finally:
        github_write.post_summary_comment = github_write_post

    assert [f["type"] for f in ledger.findings] == ["test_gap", "defect"]
    assert {f["posted_as"] for f in ledger.findings} == {"summary"}
    assert {f["confidence"] for f in ledger.findings} == {"low"}
    assert all(f["comment_id"] == 99 for f in ledger.findings)


# --- the escalation decision, recorded rather than implied -------------------


def test_escalation_reasons_are_distinguished_from_each_other():
    """Four situations used to collapse into a bare "escalated_to_human"."""
    empty = root.ReviewLedger()
    assert empty.why_escalated == "no_findings"

    suppressed = root.ReviewLedger()
    suppressed.record(type="defect", posted_as="suppressed", suppressed_reason="line_not_in_diff")
    assert suppressed.why_escalated == "all_findings_suppressed"

    uncitable = root.ReviewLedger()
    uncitable.record(type="defect", posted_as="summary", confidence="low")
    assert uncitable.why_escalated == "no_conventions_to_cite"

    cited = root.ReviewLedger()
    cited.add_evidence("Some written rule.")
    cited.record(type="defect", posted_as="summary", confidence="low")
    assert cited.why_escalated == "no_citable_findings"


def test_a_completed_review_has_no_escalation_reason():
    ledger = root.ReviewLedger()
    ledger.record(type="defect", posted_as="inline", confidence="high")
    assert ledger.outcome == "changes_requested"
    assert ledger.why_escalated is None


def test_the_agents_stated_reason_wins_over_the_derived_one():
    ledger = root.ReviewLedger()
    _, _, act = _tools(ledger)
    assign_reviewer = next(f for f in act if f.__name__ == "assign_reviewer")

    calls = {}
    original = github_write.assign_reviewer
    try:
        github_write.assign_reviewer = lambda *a, **k: calls.setdefault("r", {"assigned": ["octocat"]})
        assign_reviewer(["octocat"], "diff truncated at 400 files")
    finally:
        github_write.assign_reviewer = original

    assert ledger.why_escalated == "diff truncated at 400 files"


def test_decision_is_a_complete_audit_trail():
    """What gets persisted and logged: how it landed, not just where."""
    ledger = root.ReviewLedger()
    ledger.add_evidence("A written rule.")
    ledger.record(type="defect", posted_as="inline", confidence="high")
    ledger.record(type="convention", posted_as="suppressed", suppressed_reason="citation_not_grounded")

    decision = ledger.decision
    assert decision["outcome"] == "changes_requested"
    assert decision["posted_as"] == {"inline": 1, "suppressed": 1}
    assert decision["by_type"] == {"defect": 1, "convention": 1}
    assert decision["suppressed_reasons"] == {"citation_not_grounded": 1}
    assert decision["evidence_sources"] == 1
    assert decision["escalation_reason"] is None


def test_a_rule_full_of_inline_code_is_still_checkable():
    """Regression, from the first live run of the gate: backticks were treated
    as quote delimiters, so a verbatim quote of rule 9 was shredded into
    fragments too short to match and a correct citation was refused."""
    evidence = (
        "9. Log messages use lazy `%s` formatting - `log.info(\"saw %s\", x)`, not\n"
        "   `log.info(f\"saw {{x}}\")`."
    )
    citation = "Log messages use lazy `%s` formatting - `log.info(\"saw %s\", x)`, not\n`log.info(f\"saw {{x}}\")`."
    assert github_write.citation_is_grounded(citation, evidence)


def test_a_file_the_pr_never_touched_never_reaches_github():
    """The hole the live run walked through: an unknown path left commentable
    .get() returning None, which skipped the guard instead of tripping it."""
    ledger = root.ReviewLedger()
    _, _, act = _tools(ledger)
    post_inline_comment = next(f for f in act if f.__name__ == "post_inline_comment")

    ledger.commentable["in_the_pr.py"] = {10}
    ledger.add_evidence("Close every file handle you open.")

    result = post_inline_comment(
        "not_in_the_pr.py", 294, "Leaks a handle",
        "CONTRIBUTING.md: 'close every file handle you open'", "defect",
    )

    assert result["error"] == "file_not_in_diff"
    assert ledger.findings[0]["suppressed_reason"] == "file_not_in_diff"
    assert ledger.posted_inline == 0


def test_an_omitted_classification_is_recorded_not_raised():
    """Left required, ADK raises TypeError before the wrapper runs and the
    attempt never reaches the ledger."""
    ledger = root.ReviewLedger()
    _, _, act = _tools(ledger)
    post_inline_comment = next(f for f in act if f.__name__ == "post_inline_comment")

    ledger.commentable["a.py"] = {10}
    result = post_inline_comment("a.py", 10, "Something", "CONTRIBUTING.md: 'a written rule here'")

    assert result["error"] == "unknown_finding_type"
    assert len(ledger.findings) == 1


def test_the_model_id_has_no_default():
    """A plausible-but-wrong id would deploy clean and fail at the first Vertex
    call; build_agent's error exists to prevent that and a default hid it."""
    import config

    assert config.GEMINI_MODEL == os.environ.get("GEMINI_MODEL", "")
