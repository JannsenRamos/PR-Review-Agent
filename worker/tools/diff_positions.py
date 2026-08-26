"""Which lines of a unified diff GitHub will accept an inline comment on.

The highest-risk piece of code in the build: comment on a line outside the diff
hunk and GitHub answers 422, which on demo day looks like the agent silently
did nothing. Pure function, no I/O, unit-tested — keep it that way.

We anchor with `line` + `side` (the modern review-comment API) rather than the
legacy `position` offset, so this only has to answer "is this line commentable",
not "what offset is it".
"""

from __future__ import annotations

import re

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def commentable_lines(patch: str | None) -> set[int]:
    """Line numbers in the head file that appear as added or context in `patch`.

    Deleted lines are excluded: they do not exist on the head side, and a
    RIGHT-side comment on one is rejected.
    """
    if not patch:
        return set()

    lines: set[int] = set()
    new_line = 0
    for raw in patch.splitlines():
        hunk = _HUNK.match(raw)
        if hunk:
            new_line = int(hunk.group(1))
            continue
        if new_line == 0:
            continue  # preamble before the first hunk
        if raw.startswith("+"):
            lines.add(new_line)
            new_line += 1
        elif raw.startswith("-"):
            continue
        elif raw.startswith("\\"):
            continue  # "\ No newline at end of file"
        else:
            # Context line (leading space, or an empty line some tools emit bare).
            lines.add(new_line)
            new_line += 1
    return lines


def added_lines(patch: str | None) -> set[int]:
    """Only the lines this PR actually adds — the ones worth commenting on."""
    if not patch:
        return set()

    lines: set[int] = set()
    new_line = 0
    for raw in patch.splitlines():
        hunk = _HUNK.match(raw)
        if hunk:
            new_line = int(hunk.group(1))
            continue
        if new_line == 0:
            continue
        if raw.startswith("+"):
            lines.add(new_line)
            new_line += 1
        elif raw.startswith("-") or raw.startswith("\\"):
            continue
        else:
            new_line += 1
    return lines
