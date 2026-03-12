#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pyyaml"]
# ///
"""
Comfy Complete - Run Docker Container

This script runs the Comfy Complete container with GPU support and volume mounts.

Usage:
    ./scripts/run-container.py                    # Use ./config.yaml
    ./scripts/run-container.py ~/myconfig.yaml    # Custom config
    ./scripts/run-container.py --detach           # Run in background
    ./scripts/run-container.py --help             # Show help
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

import yaml


def expand_path(path: str) -> str:
    """Expand ~ and environment variables in a path."""
    if not path:
        return ""
    return str(Path(os.path.expandvars(os.path.expanduser(path))).resolve())


def create_dir_if_needed(dir_path: str, name: str) -> None:
    """Create directory if it doesn't exist."""
    if dir_path:
        path = Path(dir_path)
        if not path.exists():
            print(f"Creating {name} directory: {dir_path}")
            path.mkdir(parents=True, exist_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Comfy Complete - Run Docker Container",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                          # Use ./config.yaml
  %(prog)s ~/myconfig.yaml          # Use custom config
  %(prog)s --detach                 # Run in background
  %(prog)s ~/myconfig.yaml -d       # Custom config, background
""",
    )
    parser.add_argument(
        "config",
        nargs="?",
        default="./config.yaml",
        help="Path to config file (default: ./config.yaml)",
    )
    parser.add_argument(
        "--detach", "-d",
        action="store_true",
        help="Run container in background",
    )
    parser.add_argument(
        "--no-rm",
        action="store_true",
        help="Keep container after it stops",
    )

    args = parser.parse_args()

    # Check config file exists
    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}")
        print()
        print("Create a config file by copying the example:")
        print("  cp config.yaml.example config.yaml")
        return 1

    print("=== Comfy Complete - Running Container ===")
    print(f"Config file: {config_path}")
    print()

    # Load config
    with open(config_path) as f:
        config = yaml.safe_load(f)

    # Read config values with defaults
    image = config.get("image", "comfy-complete:latest")
    port = config.get("port", 8188)
    container_name = config.get("name", "comfy-complete")

    gpu_config = config.get("gpu", {})
    gpu_enabled = gpu_config.get("enabled", True)
    gpu_devices = gpu_config.get("devices", "all")

    volumes = config.get("volumes", {})
    models_path = expand_path(volumes.get("models", ""))
    output_path = expand_path(volumes.get("output", ""))
    input_path = expand_path(volumes.get("input", ""))
    workflows_path = expand_path(volumes.get("workflows", ""))

    print(f"Image: {image}")
    print(f"Port: {port}")
    print(f"Container name: {container_name}")
    print(f"GPU enabled: {gpu_enabled}")
    if gpu_enabled:
        print(f"GPU devices: {gpu_devices}")
    print()

    # Create directories if they don't exist
    create_dir_if_needed(models_path, "models")
    create_dir_if_needed(output_path, "output")
    create_dir_if_needed(input_path, "input")
    create_dir_if_needed(workflows_path, "workflows")

    # Build docker run command
    docker_cmd = ["docker", "run"]

    # Add --rm flag
    if not args.no_rm:
        docker_cmd.append("--rm")

    # Add detach flag
    if args.detach:
        docker_cmd.append("-d")

    # Add container name
    docker_cmd.extend(["--name", container_name])

    # Add GPU support
    if gpu_enabled:
        if gpu_devices == "all":
            docker_cmd.extend(["--gpus", "all"])
        else:
            docker_cmd.extend(["--gpus", f"device={gpu_devices}"])

    # Add port mapping
    docker_cmd.extend(["-p", f"{port}:8188"])

    # Add volume mounts
    if models_path:
        docker_cmd.extend(["-v", f"{models_path}:/app/comfyui/models"])
        print(f"Mounting models: {models_path}")
    if output_path:
        docker_cmd.extend(["-v", f"{output_path}:/app/comfyui/output"])
        print(f"Mounting output: {output_path}")
    if input_path:
        docker_cmd.extend(["-v", f"{input_path}:/app/comfyui/input"])
        print(f"Mounting input: {input_path}")
    if workflows_path:
        docker_cmd.extend(["-v", f"{workflows_path}:/app/comfyui/user/default/workflows"])
        print(f"Mounting workflows: {workflows_path}")

    # Add image name
    docker_cmd.append(image)

    print()
    print("Running container...")
    print(f"Command: {' '.join(docker_cmd)}")
    print()

    # Run the container
    result = subprocess.run(docker_cmd)

    if args.detach:
        print()
        print("Container started in background.")
        print(f"View logs: docker logs -f {container_name}")
        print(f"Stop: docker stop {container_name}")
    else:
        print()
        print("Container stopped.")

    print()
    print(f"Access ComfyUI at: http://localhost:{port}")

    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
