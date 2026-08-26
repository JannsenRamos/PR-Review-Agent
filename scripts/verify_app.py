"""Verify the GitHub App can authenticate and has the permissions it needs.

Read-only: mints a token, inspects the installation, fetches a PR. Posts
nothing. Run this before deploying, so a missing permission surfaces now rather
than as a 403 halfway through a live demo.

    py -3.12 scripts/verify_app.py --app-id 123456 --key ~/pr-agent.pem --repo owner/name

The private key is read to sign a JWT locally and is never printed or sent
anywhere except GitHub's token endpoint.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx
import jwt

API = "https://api.github.com"

# permission -> minimum acceptable level, and what breaks without it
REQUIRED = {
    "pull_requests": ("write", "inline comments, request changes, assign reviewer"),
    "issues": ("write", "summary comment and labels"),
    "contents": ("read", "CONTRIBUTING.md, lint config, file context"),
    "checks": ("read", "the CI gate"),
    "metadata": ("read", "repo lookup"),
}
RANK = {"read": 1, "write": 2, "admin": 3}


def app_jwt(app_id: str, key_path: Path) -> str:
    now = int(time.time())
    return jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": app_id},
        key_path.read_text(),
        algorithm="RS256",
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-id", required=True)
    ap.add_argument("--key", required=True, type=Path)
    ap.add_argument("--repo", required=True, help="owner/name")
    args = ap.parse_args()

    if not args.key.exists():
        print(f"FAIL  private key not found: {args.key}", file=sys.stderr)
        return 2

    jwt_headers = {
        "Authorization": f"Bearer {app_jwt(args.app_id, args.key)}",
        "Accept": "application/vnd.github+json",
    }

    app = httpx.get(f"{API}/app", headers=jwt_headers, timeout=15.0)
    if app.status_code != 200:
        print(f"FAIL  app JWT rejected ({app.status_code}): {app.text[:200]}", file=sys.stderr)
        print("      Check the App ID matches the private key.", file=sys.stderr)
        return 1
    print(f"OK    authenticated as app '{app.json()['slug']}'")

    owner, name = args.repo.split("/", 1)
    inst = httpx.get(f"{API}/repos/{owner}/{name}/installation", headers=jwt_headers, timeout=15.0)
    if inst.status_code != 200:
        print(f"FAIL  app is not installed on {args.repo} ({inst.status_code})", file=sys.stderr)
        return 1
    installation = inst.json()
    installation_id = installation["id"]
    print(f"OK    installed on {args.repo} (installation_id={installation_id})")

    granted = installation.get("permissions", {})
    missing = []
    for perm, (needed, why) in REQUIRED.items():
        have = granted.get(perm)
        if not have or RANK.get(have, 0) < RANK[needed]:
            missing.append(f"      - {perm}: need {needed}, have {have or 'none'}  ({why})")
        else:
            print(f"OK    {perm}: {have}")
    if missing:
        print("FAIL  missing permissions:", file=sys.stderr)
        print("\n".join(missing), file=sys.stderr)
        print("      Fix under App settings -> Permissions & events, then accept the", file=sys.stderr)
        print("      permission update on the installation.", file=sys.stderr)
        return 1

    events = installation.get("events", [])
    if "pull_request" not in events:
        print(f"FAIL  not subscribed to pull_request (subscribed: {events or 'none'})", file=sys.stderr)
        return 1
    print(f"OK    subscribed to: {', '.join(events)}")

    token = httpx.post(
        f"{API}/app/installations/{installation_id}/access_tokens",
        headers=jwt_headers,
        timeout=15.0,
    )
    if token.status_code != 201:
        print(f"FAIL  could not mint installation token ({token.status_code})", file=sys.stderr)
        return 1
    print("OK    minted installation token")

    auth = {
        "Authorization": f"Bearer {token.json()['token']}",
        "Accept": "application/vnd.github+json",
    }

    prs = httpx.get(
        f"{API}/repos/{args.repo}/pulls",
        headers=auth,
        params={"state": "all", "per_page": 1},
        timeout=15.0,
    )
    if prs.status_code != 200:
        print(f"FAIL  cannot list pull requests ({prs.status_code})", file=sys.stderr)
        return 1

    found = prs.json()
    if not found:
        print("OK    token works. No pull requests yet — open one to exercise the diff path.")
        return 0

    pr = found[0]
    files = httpx.get(
        f"{API}/repos/{args.repo}/pulls/{pr['number']}/files", headers=auth, timeout=15.0
    )
    if files.status_code != 200:
        print(f"FAIL  cannot read diff of PR #{pr['number']} ({files.status_code})", file=sys.stderr)
        return 1
    print(f"OK    read diff of PR #{pr['number']}: {len(files.json())} changed file(s)")

    checks = httpx.get(
        f"{API}/repos/{args.repo}/commits/{pr['head']['sha']}/check-runs", headers=auth, timeout=15.0
    )
    if checks.status_code != 200:
        print(f"FAIL  cannot read check-runs ({checks.status_code}) — the CI gate needs this", file=sys.stderr)
        return 1
    print(f"OK    read CI check-runs: {checks.json().get('total_count', 0)} run(s)")

    print("\nAll good. GitHub App auth is proven; only GCP setup remains.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
