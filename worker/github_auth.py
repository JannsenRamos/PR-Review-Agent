"""GitHub authentication: App installation tokens, with a PAT fallback.

Callers use `get_token(installation_id)` and must not care which mode is active.
Mode is chosen by env: GITHUB_APP_ID + GITHUB_PRIVATE_KEY present -> App, else PAT.

This file is duplicated into worker/ by design (see PRD/implementation-plan-v1.md
section 1): two Cloud Run images, no shared package to build and version.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import httpx
import jwt

GITHUB_API = os.environ.get("GITHUB_API", "https://api.github.com")

# Installation tokens last an hour. Minting one costs a round trip, which the
# receiver cannot afford on its hot path, so cache until shortly before expiry.
_TOKEN_CACHE: dict[str, "_CachedToken"] = {}
_EXPIRY_MARGIN_S = 300


@dataclass
class _CachedToken:
    token: str
    expires_at: float

    @property
    def usable(self) -> bool:
        return time.time() < self.expires_at - _EXPIRY_MARGIN_S


def _app_jwt() -> str:
    app_id = os.environ["GITHUB_APP_ID"]
    private_key = os.environ["GITHUB_PRIVATE_KEY"]
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 540, "iss": app_id}
    return jwt.encode(payload, private_key, algorithm="RS256")


def using_app_auth() -> bool:
    return bool(os.environ.get("GITHUB_APP_ID") and os.environ.get("GITHUB_PRIVATE_KEY"))


def get_token(installation_id: int | str | None = None) -> str:
    """Return a bearer token for the GitHub REST API.

    Falls back to GITHUB_PAT when App credentials are not configured. The PAT
    path is the timeboxed Day 1 escape hatch from the implementation plan.
    """
    if not using_app_auth():
        pat = os.environ.get("GITHUB_PAT")
        if not pat:
            raise RuntimeError(
                "No GitHub credentials: set GITHUB_APP_ID + GITHUB_PRIVATE_KEY, or GITHUB_PAT"
            )
        return pat

    if installation_id is None:
        raise RuntimeError("App auth requires an installation_id")

    key = str(installation_id)
    cached = _TOKEN_CACHE.get(key)
    if cached and cached.usable:
        return cached.token

    response = httpx.post(
        f"{GITHUB_API}/app/installations/{key}/access_tokens",
        headers={
            "Authorization": f"Bearer {_app_jwt()}",
            "Accept": "application/vnd.github+json",
        },
        timeout=10.0,
    )
    response.raise_for_status()
    body = response.json()

    # expires_at is ISO8601; the exact value matters less than not reusing a
    # token past its life, so treat it as one hour from now.
    _TOKEN_CACHE[key] = _CachedToken(token=body["token"], expires_at=time.time() + 3600)
    return body["token"]


def api_headers(installation_id: int | str | None = None) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {get_token(installation_id)}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
