"""Replay a saved webhook payload against a locally running receiver.

The inner dev loop: iterate in seconds instead of round-tripping through GitHub.

    py -3.12 scripts/replay.py fixtures/pull_request.opened.json

Signs the payload with GITHUB_WEBHOOK_SECRET from the environment, so the
receiver's real signature check runs — that path stays exercised locally.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import sys
import uuid
from pathlib import Path

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=Path)
    parser.add_argument("--url", default="http://localhost:8080/webhook")
    parser.add_argument("--event", default="pull_request")
    args = parser.parse_args()

    secret = os.environ.get("GITHUB_WEBHOOK_SECRET")
    if not secret:
        print("GITHUB_WEBHOOK_SECRET is not set", file=sys.stderr)
        return 2

    body = args.payload.read_bytes()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    response = httpx.post(
        args.url,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": args.event,
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": signature,
        },
        timeout=30.0,
    )
    print(f"{response.status_code} {response.text[:500]}")
    return 0 if response.status_code < 400 else 1


if __name__ == "__main__":
    raise SystemExit(main())
