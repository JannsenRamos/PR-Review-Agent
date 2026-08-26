# Contributing

Rules are numbered so they can be cited. The review agent quotes them verbatim
in inline comments and will not invent a convention that is not written here —
if a rule is not in this file, the most the agent can do is ask a question about
it in the summary comment.

Keep rules checkable from a diff alone. A rule that needs the whole repo in
context to evaluate is a rule nothing can enforce.

## Error handling

1. Every outbound HTTP call must check the response status before parsing the
   body. Use `raise_for_status()`, or check `status_code` explicitly.
2. Never use a bare `except:`. Catch the specific exception, or `except
   Exception` paired with `log.exception(...)`.
3. Agent tools must return a dict with an `error` key on failure, never raise.
   The agent has to be able to see a failure and route around it.
4. Functions that can fail must return an error value or raise. Never return
   `None` to signal failure.

## Resources and I/O

5. Every network call passes an explicit `timeout=`. There is no acceptable
   default timeout.
6. Files, sockets and clients are opened with a `with` block.

## Conventions

7. Module-level constants are `UPPER_SNAKE_CASE`. Nothing else at module level
   is mutable.
8. No magic strings for configuration. Every tunable is read through
   `worker/config.py`, including model ids, collection names and thresholds.
9. Log messages use lazy `%s` formatting — `log.info("saw %s", x)`, not
   `log.info(f"saw {x}")`.
10. Secrets are read from the environment, populated from Secret Manager at
    deploy time. Never read a credential from a file path in application code.

## Tests

11. A new conditional branch or error path needs a test covering it.
12. Tests must not make network calls or require cloud credentials. Anything
    needing a live model belongs in a demo run, not the test suite.

## Invariants

These are load-bearing. A change that violates one is wrong even if it passes
review and CI.

13. The agent has no tool to approve a pull request, and must not be given one.
    "Never approves" is enforced by the absence of the tool.
14. Inline comments require a citation. The check lives in the
    `post_inline_comment` wrapper, not only in the prompt — a prompt drifts under
    load, a wrapper does not.
15. The Firestore review document shape is a public contract
    (`infra/firestore-schema.md`). Changing it is a breaking change.
