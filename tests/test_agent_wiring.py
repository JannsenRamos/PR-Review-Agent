"""Offline checks on the agent wiring.

No Vertex call and no GitHub call: these verify the structure and the guardrails,
which are the parts that must not quietly regress. Anything needing a real model
belongs in the live demo run, not here.

    py -3.12 -m pytest tests/ -q
"""

from __future__ import annotations

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
    result = post_inline_comment("a.py", 99, "Unhandled error", "CONTRIBUTING.md: handle errors")

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
