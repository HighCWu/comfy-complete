#!/usr/bin/env python3
"""
Comfy Complete - Build Docker Container

A Python-based build system for the Comfy Complete Docker image.

Usage:
    ./scripts/build-container.py <image-name> [options]

Options:
    --dev            Development mode: enables uv cache, verbose progress
    --no-cache       Build without using Docker cache
    --push           Push to registry after building
    --platform PLAT  Target platform (e.g., linux/amd64)
    --build-config   Path to build-config.yaml (optional)
    --help           Show this help message

Examples:
    ./scripts/build-container.py comfy-complete
    ./scripts/build-container.py comfy-complete --dev
    ./scripts/build-container.py comfy-complete:v1.0.0
    ./scripts/build-container.py myregistry/comfy-complete:latest --push
    ./scripts/build-container.py comfy-complete --no-cache --platform linux/amd64
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


def load_yaml(path: Path) -> dict[str, Any]:
    """Load and parse a YAML file."""
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def check_docker() -> bool:
    """Check if Docker is installed and available."""
    return shutil.which("docker") is not None


def build_docker_command(
    image_name: str,
    dockerfile_path: Path,
    context_path: Path,
    dev_mode: bool = False,
    no_cache: bool = False,
    platform: str | None = None,
    build_args: dict[str, str] | None = None,
) -> list[str]:
    """Build the Docker command list."""
    cmd = ["docker", "build"]

    if dev_mode:
        # Dev mode: enable uv caching and show detailed progress
        cmd.extend(["--build-arg", "UV_CACHE=1", "--progress=plain", "--network=host"])

    if no_cache:
        cmd.append("--no-cache")

    if platform:
        cmd.extend(["--platform", platform])

    if build_args:
        for key, value in build_args.items():
            cmd.extend(["--build-arg", f"{key}={value}"])

    cmd.extend(["-t", image_name])
    cmd.extend(["-f", str(dockerfile_path)])
    cmd.append(str(context_path))

    return cmd


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Comfy Complete - Build Docker Container",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s comfy-complete
  %(prog)s comfy-complete --dev
  %(prog)s comfy-complete:v1.0.0
  %(prog)s myregistry/comfy-complete:latest --push
  %(prog)s comfy-complete --no-cache --platform linux/amd64
        """,
    )

    parser.add_argument(
        "image_name",
        help="Name for the Docker image (e.g., comfy-complete:latest)",
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Development mode: enables uv package caching and verbose progress",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Build without using Docker cache",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Push to registry after building",
    )
    parser.add_argument(
        "--platform",
        type=str,
        help="Target platform (e.g., linux/amd64)",
    )
    parser.add_argument(
        "--build-config",
        type=Path,
        default=None,
        help="Path to build-config.yaml (optional)",
    )

    args = parser.parse_args()

    # Determine paths
    script_dir = Path(__file__).parent
    repo_root = script_dir.parent
    dockerfile_path = repo_root / "docker" / "Dockerfile"

    print("=== Comfy Complete - Building Docker Image ===")
    print(f"Repository root: {repo_root}")
    print(f"Image name: {args.image_name}")
    print()

    # Check for Docker
    if not check_docker():
        print("Error: docker is required but not installed", file=sys.stderr)
        return 1

    # Load build config
    config_path = args.build_config or repo_root / "build-config.yaml"
    build_config = load_yaml(config_path)

    # Extract build options from config
    build_section = build_config.get("build", {})
    remove_manager = build_section.get("remove_manager", False)

    # Prepare build args
    build_args = {
        "REMOVE_MANAGER": "1" if remove_manager else "0",
    }

    # Print mode info
    if args.dev:
        print("Mode: development (uv cache enabled, verbose progress)")
    else:
        print("Mode: production")

    if args.no_cache:
        print("Docker cache: disabled")
    else:
        print("Docker cache: enabled")

    if args.platform:
        print(f"Platform: {args.platform}")

    if remove_manager:
        print("Manager removal: enabled")

    print()

    # Enable BuildKit for cache mount support
    os.environ["DOCKER_BUILDKIT"] = "1"

    # Build the command
    cmd = build_docker_command(
        image_name=args.image_name,
        dockerfile_path=dockerfile_path,
        context_path=repo_root,
        dev_mode=args.dev,
        no_cache=args.no_cache,
        platform=args.platform,
        build_args=build_args,
    )

    print("Building image...")
    print(f"Command: {' '.join(cmd)}")
    print()

    # Run the build
    result = subprocess.run(cmd)

    if result.returncode != 0:
        print()
        print("Build failed!", file=sys.stderr)
        return result.returncode

    print()
    print("=== Build Complete ===")
    print(f"Image: {args.image_name}")

    # Push if requested
    if args.push:
        print()
        print("Pushing image to registry...")
        push_result = subprocess.run(["docker", "push", args.image_name])
        if push_result.returncode != 0:
            print("Push failed!", file=sys.stderr)
            return push_result.returncode
        print("Push complete!")

    print()
    print("To run the container:")
    print(f"  docker run -p 8188:8188 {args.image_name}")
    print()
    print("To run with a models volume:")
    print(f"  docker run -p 8188:8188 -v /path/to/models:/app/comfyui/models {args.image_name}")
    print()
    print("To run with GPU support:")
    print(f"  docker run --gpus all -p 8188:8188 {args.image_name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
