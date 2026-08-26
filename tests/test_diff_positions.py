"""Unit tests for the diff line mapper.

This is the piece most likely to fail silently in front of judges: a comment on
a line outside the diff hunk gets a 422 and the agent looks like it did nothing.
No network, no credentials — these run anywhere.

    py -3.12 -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))

from tools.diff_positions import added_lines, commentable_lines  # noqa: E402

SIMPLE = """@@ -1,4 +1,6 @@
 import os
+import sys

-def run():
+def run(retries=3):
+    print(retries)
     pass
"""

MULTI_HUNK = """@@ -10,3 +10,4 @@ def alpha():
     a = 1
+    b = 2
     return a
@@ -40,2 +41,3 @@ def beta():
     x = 0
+    y = 1
"""


def test_added_lines_simple():
    # +import sys -> 2; +def run(retries=3) -> 4; +print(retries) -> 5
    assert added_lines(SIMPLE) == {2, 4, 5}


def test_context_lines_are_commentable_but_not_added():
    commentable = commentable_lines(SIMPLE)
    assert added_lines(SIMPLE) <= commentable
    assert 1 in commentable  # " import os" is context
    assert 1 not in added_lines(SIMPLE)


def test_deleted_lines_never_appear():
    # "-def run():" exists only on the left side; commenting RIGHT on it 422s.
    assert commentable_lines(SIMPLE) == {1, 2, 3, 4, 5, 6}


def test_multiple_hunks_restart_numbering():
    assert added_lines(MULTI_HUNK) == {11, 42}


def test_line_outside_any_hunk_is_rejected():
    assert 999 not in commentable_lines(MULTI_HUNK)


def test_empty_patch_is_safe():
    # Binary files and renames come back with patch=None.
    assert commentable_lines(None) == set()
    assert added_lines("") == set()
