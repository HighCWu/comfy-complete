#!/usr/bin/env python3
"""Clone custom node packs for PR review analysis.

This module handles fetching node pack repositories from either the ComfyUI
registry or direct GitHub URLs for security and label analysis.

Supports two formats:
    1. Registry nodes: name + version (e.g., comfyui-kjnodes @ 1.1.6)
    2. GitHub URL nodes: https://github.com/user/repo@commit

Usage:
    python clone_nodes.py --changes changes.json --dest /tmp/nodes
    python clone_nodes.py --node comfyui-kjnodes --version 1.1.6 --dest /tmp/nodes
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class RegistryInfo:
    """Information retrieved from the ComfyUI registry."""

    repository_url: str
    latest_version: str


@dataclass
class GitHubRef:
    """Parsed GitHub URL reference."""

    repo_url: str
    commit: str
    repo_name: str


@dataclass
class CloneResult:
    """Result of a clone operation."""

    name: str
    path: Path
    success: bool
    error: str | None = None


def query_registry(node_name: str) -> RegistryInfo | None:
    """Query the ComfyUI registry API for node pack information.

    Args:
        node_name: Name of the node pack in the registry

    Returns:
        RegistryInfo if found, None otherwise
    """
    api_url = f"https://api.comfy.org/nodes/{node_name}"

    try:
        with urlopen(api_url, timeout=30) as response:
            data = json.loads(response.read().decode())
            repo_url = data.get("repository", "")
            latest_version = data.get("latest_version", {}).get("version", "")

            if repo_url:
                return RegistryInfo(
                    repository_url=repo_url,
                    latest_version=latest_version,
                )
    except (URLError, json.JSONDecodeError, KeyError) as e:
        logger.warning("Failed to query registry for %s: %s", node_name, e)

    return None


def parse_github_url(url: str) -> GitHubRef | None:
    """Parse a GitHub URL with commit hash.

    Expected format: https://github.com/user/repo@commitsha

    Args:
        url: GitHub URL with commit reference

    Returns:
        GitHubRef if valid, None otherwise
    """
    match = re.match(r"https://github\.com/([^@]+)@([a-f0-9]+)", url)
    if not match:
        return None

    repo_path, commit = match.groups()
    repo_name = repo_path.split("/")[-1]

    return GitHubRef(
        repo_url=f"https://github.com/{repo_path}",
        commit=commit,
        repo_name=repo_name,
    )


def run_git_command(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run a git command and return the result.

    Args:
        args: Git command arguments
        cwd: Working directory for the command

    Returns:
        CompletedProcess with stdout/stderr captured
    """
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def clone_from_registry(node_name: str, version: str, dest_dir: Path) -> CloneResult:
    """Clone a node pack from the ComfyUI registry.

    Args:
        node_name: Registry name of the node pack
        version: Desired version (empty string for latest)
        dest_dir: Destination directory for cloning

    Returns:
        CloneResult indicating success or failure
    """
    registry_info = query_registry(node_name)
    if not registry_info:
        return CloneResult(
            name=node_name,
            path=dest_dir,
            success=False,
            error=f"Node pack '{node_name}' not found in registry",
        )

    repo_url = registry_info.repository_url
    target_version = version or registry_info.latest_version
    repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
    dest_path = dest_dir / repo_name

    if dest_path.exists():
        logger.info("Skipping %s: already exists at %s", node_name, dest_path)
        return CloneResult(name=node_name, path=dest_path, success=True)

    logger.info("Cloning %s from %s...", node_name, repo_url)

    # Try version tags in order of likelihood
    tag_patterns = [f"v{target_version}", target_version, f"release-{target_version}"]

    for tag in tag_patterns:
        result = run_git_command(
            ["clone", "--depth", "1", "--branch", tag, repo_url, str(dest_path)]
        )
        if result.returncode == 0:
            logger.info("Cloned %s at tag %s", node_name, tag)
            return CloneResult(name=node_name, path=dest_path, success=True)

    # Fallback to default branch
    logger.warning("No version tag found for %s, cloning default branch", node_name)
    result = run_git_command(["clone", "--depth", "1", repo_url, str(dest_path)])

    if result.returncode == 0:
        return CloneResult(name=node_name, path=dest_path, success=True)

    return CloneResult(
        name=node_name,
        path=dest_path,
        success=False,
        error=f"Clone failed: {result.stderr}",
    )


def clone_from_github_url(url: str, dest_dir: Path) -> CloneResult:
    """Clone a node pack from a GitHub URL with commit hash.

    Args:
        url: GitHub URL in format https://github.com/user/repo@commit
        dest_dir: Destination directory for cloning

    Returns:
        CloneResult indicating success or failure
    """
    github_ref = parse_github_url(url)
    if not github_ref:
        return CloneResult(
            name=url,
            path=dest_dir,
            success=False,
            error=f"Invalid GitHub URL format: {url}",
        )

    dest_path = dest_dir / github_ref.repo_name

    if dest_path.exists():
        logger.info("Skipping %s: already exists at %s", github_ref.repo_name, dest_path)
        return CloneResult(name=url, path=dest_path, success=True)

    logger.info(
        "Cloning %s from %s at %s...",
        github_ref.repo_name,
        github_ref.repo_url,
        github_ref.commit[:8],
    )

    # Clone the repository
    result = run_git_command(["clone", github_ref.repo_url, str(dest_path)])
    if result.returncode != 0:
        return CloneResult(
            name=url,
            path=dest_path,
            success=False,
            error=f"Clone failed: {result.stderr}",
        )

    # Checkout specific commit
    result = run_git_command(["checkout", github_ref.commit], cwd=dest_path)
    if result.returncode != 0:
        return CloneResult(
            name=url,
            path=dest_path,
            success=False,
            error=f"Checkout failed: {result.stderr}",
        )

    logger.info("Cloned %s at commit %s", github_ref.repo_name, github_ref.commit[:8])
    return CloneResult(name=url, path=dest_path, success=True)


def clone_node_pack(pack: dict[str, Any], dest_dir: Path) -> CloneResult | None:
    """Clone a node pack based on its configuration.

    Args:
        pack: Node pack configuration dictionary
        dest_dir: Destination directory for cloning

    Returns:
        CloneResult if cloning was attempted, None for skipped packs
    """
    name = pack.get("name", "")
    version = pack.get("version", "")

    # Skip core nodes - they're part of ComfyUI itself
    if name == "core":
        return None

    if name.startswith("https://github.com/"):
        return clone_from_github_url(name, dest_dir)

    if name:
        return clone_from_registry(name, version, dest_dir)

    return None


def process_changes_file(changes_path: Path, dest_dir: Path) -> list[CloneResult]:
    """Process a changes.json file and clone all affected node packs.

    Args:
        changes_path: Path to the changes.json file
        dest_dir: Destination directory for cloning

    Returns:
        List of CloneResults for all attempted clones
    """
    with changes_path.open() as f:
        changes = json.load(f)

    results = []

    # Clone new node packs
    for pack in changes.get("new", []):
        result = clone_node_pack(pack, dest_dir)
        if result:
            results.append(result)

    # Clone updated node packs (use head version)
    for update in changes.get("updated", []):
        pack = update.get("head", update)
        pack["name"] = update.get("name", pack.get("name", ""))
        result = clone_node_pack(pack, dest_dir)
        if result:
            results.append(result)

    return results


def main() -> int:
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(
        description="Clone custom node packs for PR review",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--changes",
        type=Path,
        help="JSON file with detected changes from detect_changes.py",
    )
    parser.add_argument(
        "--node",
        help="Single node name or GitHub URL to clone",
    )
    parser.add_argument(
        "--version",
        default="",
        help="Version for registry nodes",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        required=True,
        help="Destination directory for cloned repos",
    )
    args = parser.parse_args()

    # Create destination directory
    args.dest.mkdir(parents=True, exist_ok=True)

    results: list[CloneResult] = []

    if args.changes:
        results = process_changes_file(args.changes, args.dest)
    elif args.node:
        if args.node.startswith("https://github.com/"):
            result = clone_from_github_url(args.node, args.dest)
        else:
            result = clone_from_registry(args.node, args.version, args.dest)
        results.append(result)
    else:
        parser.error("Either --changes or --node must be specified")

    # Output results as JSON
    output = {
        "cloned": [
            {"name": r.name, "path": str(r.path), "success": r.success, "error": r.error}
            for r in results
        ],
        "summary": {
            "total": len(results),
            "successful": sum(1 for r in results if r.success),
            "failed": sum(1 for r in results if not r.success),
        },
    }
    print(json.dumps(output, indent=2))

    # Return success if at least one clone succeeded
    return 0 if any(r.success for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
