# fixtures/

`pull_request.opened.json` is a hand-written minimum: only the fields the
receiver reads. It is enough to exercise signature checking, event filtering and
the publish path, and not enough to trust for anything else.

**Replace it with a real captured payload the first time one arrives.** GitHub →
your App → Advanced → Recent Deliveries → copy the request body. Real payloads
carry fields you will end up wanting (labels, base ref, author association), and
a fixture that diverges from production is a bug waiting to happen.
