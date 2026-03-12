#!/usr/bin/env python3
"""Ephemeral bridge: send a repository_dispatch to trigger ephemeral testing.

This script formats and sends the repository_dispatch event that the
ephemeral-test GitHub Actions workflow sends automatically. It is useful
for local testing and debugging.

Usage:
    # Dry run (prints payload without sending)
    python scripts/ephemeral_bridge.py --pr 123 --branch feature/my-node --dry-run

    # Send dispatch (requires GITHUB_TOKEN with repo scope on cloud repo)
    export GITHUB_TOKEN=ghp_xxxx
    python scripts/ephemeral_bridge.py --pr 123 --branch feature/my-node

Environment variables:
    GITHUB_TOKEN    PAT with repo scope on the target cloud repo
    CLOUD_REPO      Target repo (default: Comfy-Org/cloud)
    SOURCE_REPO     Source repo (default: Comfy-Org/comfy-complete)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request


def get_head_sha() -> str:
    """Get the HEAD commit SHA from git."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return "unknown"
    return result.stdout.strip()


def build_payload(
    pr_number: int,
    branch: str,
    sha: str,
    source_repo: str,
) -> dict:
    """Build the repository_dispatch payload.

    Args:
        pr_number: The PR number in comfy-complete.
        branch: The PR branch name.
        sha: The HEAD commit SHA of the PR branch.
        source_repo: The source repository (owner/repo).

    Returns:
        The full dispatch request body as a dictionary.
    """
    return {
        "event_type": "ephemeral-test",
        "client_payload": {
            "pr_number": pr_number,
            "pr_branch": branch,
            "pr_head_sha": sha,
            "source_repo": source_repo,
        },
    }


def send_dispatch(token: str, cloud_repo: str, payload: dict) -> None:
    """Send the repository_dispatch event to the cloud repo.

    Args:
        token: GitHub PAT with repo scope.
        cloud_repo: Target repository in owner/repo format.
        payload: The request body dictionary.

    Raises:
        SystemExit: If the request fails.
    """
    url = f"https://api.github.com/repos/{cloud_repo}/dispatches"
    data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    try:
        with urllib.request.urlopen(req) as response:
            # 204 No Content is the expected success response
            if response.status in (200, 204):
                print(f"Dispatch sent successfully to {cloud_repo}")
            else:
                print(f"Unexpected status: {response.status}", file=sys.stderr)
                sys.exit(1)
    except urllib.error.HTTPError as e:
        print(f"HTTP error {e.code}: {e.reason}", file=sys.stderr)
        if e.code == 404:
            print(
                "Check that GITHUB_TOKEN has 'repo' scope and the target repo exists.",
                file=sys.stderr,
            )
        elif e.code == 401:
            print("Authentication failed. Check your GITHUB_TOKEN.", file=sys.stderr)
        sys.exit(1)


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Send repository_dispatch for ephemeral testing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--pr",
        type=int,
        required=True,
        help="PR number in comfy-complete",
    )
    parser.add_argument(
        "--branch",
        required=True,
        help="PR branch name",
    )
    parser.add_argument(
        "--sha",
        default=None,
        help="Commit SHA (default: current HEAD)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print payload without sending",
    )
    parser.add_argument(
        "--cloud-repo",
        default=None,
        help="Target cloud repo (default: env CLOUD_REPO or Comfy-Org/cloud)",
    )
    parser.add_argument(
        "--source-repo",
        default=None,
        help="Source repo (default: env SOURCE_REPO or Comfy-Org/comfy-complete)",
    )
    args = parser.parse_args()

    cloud_repo = args.cloud_repo or os.environ.get("CLOUD_REPO", "Comfy-Org/cloud")
    source_repo = args.source_repo or os.environ.get(
        "SOURCE_REPO", "Comfy-Org/comfy-complete"
    )
    sha = args.sha or get_head_sha()

    payload = build_payload(
        pr_number=args.pr,
        branch=args.branch,
        sha=sha,
        source_repo=source_repo,
    )

    if args.dry_run:
        print("=== Dry Run ===")
        print(f"Target: https://api.github.com/repos/{cloud_repo}/dispatches")
        print(f"Payload:\n{json.dumps(payload, indent=2)}")
        return 0

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print(
            "GITHUB_TOKEN environment variable is required (unless --dry-run).",
            file=sys.stderr,
        )
        print(
            "The token needs 'repo' scope on the target cloud repo.",
            file=sys.stderr,
        )
        return 1

    send_dispatch(token, cloud_repo, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
