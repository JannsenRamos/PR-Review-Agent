"""Deliberately failing test, used to rehearse the CI gate.

The receiver halts a review when CI is red and the worker re-checks a deferred
job before spending a run. Neither had ever executed against a real check run,
because the repo had no CI. This file makes the suite fail on purpose so the
gate can be observed end to end.

Not for merge. Delete the branch once the gate is proven.
"""

from __future__ import annotations


def test_this_fails_on_purpose_to_turn_ci_red():
    assert 1 == 2, "intentional failure: rehearsing the review agent's CI gate"
