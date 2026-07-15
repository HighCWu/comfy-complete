#!/usr/bin/env python3
"""
Comfy Complete - Custom Node Installation Script

This script installs custom nodes defined in supported_nodes.yaml into a ComfyUI installation.
It can be used both standalone and within Docker containers. Uses uv by default for fast
package installation.

Usage:
    # Standalone (provide path to ComfyUI installation)
    python install_custom_nodes.py --comfy-path /path/to/ComfyUI

    # In Docker (uses default paths)
    python install_custom_nodes.py

    # With custom config file
    python install_custom_nodes.py --config /path/to/custom_nodes.yaml

    # Production build (skip dependency installation)
    python install_custom_nodes.py --no-deps

    # Use pip instead of uv
    python install_custom_nodes.py --no-uv
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Number of retries for transient network failures during node install.
# Git clone / registry fetch can fail with "Remote end closed connection"
# or similar errors that resolve on retry.
MAX_INSTALL_RETRIES = 3
RETRY_BASE_DELAY = 5  # seconds; doubled each retry

# Check for yaml support
try:
    import yaml
except ImportError:
    print("PyYAML not found. Installing...")
    # Try uv first, fall back to pip
    if shutil.which("uv"):
        subprocess.run(["uv", "pip", "install", "pyyaml"], check=True)
    else:
        subprocess.run([sys.executable, "-m", "pip", "install", "pyyaml"], check=True)
    import yaml


def find_comfy_cli():
    """Find the comfy-cli executable."""
    comfy_path = shutil.which("comfy")
    if comfy_path:
        return comfy_path

    # Try common locations
    possible_paths = [
        os.path.expanduser("~/Library/Python/3.12/bin/comfy"),
        os.path.expanduser("~/.local/bin/comfy"),
        "/usr/local/bin/comfy",
        "/opt/conda/bin/comfy",
    ]
    for path in possible_paths:
        if os.path.exists(path):
            return path

    return None


def find_config_file(config_arg: str | None) -> str | None:
    """Find the supported_nodes.yaml config file."""
    if config_arg and os.path.exists(config_arg):
        return config_arg

    # Try different paths relative to this script
    script_dir = Path(__file__).parent
    repo_root = script_dir.parent

    config_paths = [
        repo_root / "supported_nodes.yaml",  # Comfy Complete repo root
        Path("/app/supported_nodes.yaml"),  # Docker path
        script_dir / "supported_nodes.yaml",  # Same directory as script
    ]

    for config_path in config_paths:
        if config_path.exists():
            return str(config_path)

    return None


def load_config(config_path: str) -> dict:
    """Load and parse the YAML config file."""
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def install_comfyui_manager(comfy_path: str, pip_cmd: list[str]) -> bool:
    """Install ComfyUI Manager if not present."""
    manager_path = os.path.join(comfy_path, "custom_nodes", "ComfyUI-Manager")

    if os.path.exists(manager_path):
        print("ComfyUI Manager already installed")
        return True

    print("ComfyUI Manager not found, installing...")
    result = subprocess.run(
        ["git", "clone", "https://github.com/ltdrdata/ComfyUI-Manager.git", manager_path],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Error cloning ComfyUI Manager: {result.stderr}")
        return False

    # Install requirements if they exist
    requirements_path = os.path.join(manager_path, "requirements.txt")
    if os.path.exists(requirements_path):
        print("Installing ComfyUI Manager requirements...")
        cmd = pip_cmd + ["install", "-r", requirements_path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Warning: Error installing ComfyUI Manager requirements: {result.stderr}")

    print("ComfyUI Manager installed successfully")
    return True


def _list_custom_node_dirs(comfy_path: str) -> set[str]:
    """Return set of directory names in custom_nodes/."""
    cn_dir = os.path.join(comfy_path, "custom_nodes")
    if not os.path.exists(cn_dir):
        return set()
    return {
        item
        for item in os.listdir(cn_dir)
        if os.path.isdir(os.path.join(cn_dir, item)) and not item.startswith(".")
    }


def install_custom_nodes(
    config: dict,
    comfy_cli_path: str,
    comfy_path: str,
    no_deps: bool = False,
) -> tuple[list[str], list[dict]]:
    """Install custom nodes using comfy-cli."""
    successful = []
    failed = []

    for node_pack in config.get("node_packs", []):
        name = node_pack.get("name", "")

        # Skip core and ComfyUI Manager entries
        if name.lower() in ["comfyui-manager", "comfyui_manager", "core"]:
            continue

        version = node_pack.get("version", "")
        if version:
            node_spec = f"{name}@{version}"
        else:
            node_spec = name

        cmd = [comfy_cli_path, f"--workspace={comfy_path}", "--skip-prompt", "node", "install", node_spec]
        if no_deps:
            cmd.append("--no-deps")

        print(f"Installing {name}...")
        print(f"  Command: {' '.join(cmd)}")

        installed = False
        # Snapshot once before all retry attempts — used to detect partial
        # clones left behind by a failed attempt so they can be cleaned up.
        original_dirs = _list_custom_node_dirs(comfy_path)
        for attempt in range(1, MAX_INSTALL_RETRIES + 1):
            result = subprocess.run(cmd, capture_output=True, text=True)

            # Verify a new directory was actually created — comfy-cli may
            # return exit 0 even when the install fails (registry miss,
            # clone error, etc.).  Comparing against the original snapshot
            # (before any attempts) is robust across retries.
            dirs_after = _list_custom_node_dirs(comfy_path)
            new_dirs = dirs_after - original_dirs

            if result.returncode == 0 and new_dirs:
                print(f"  SUCCESS (created: {', '.join(sorted(new_dirs))})")
                successful.append(name)
                installed = True
                break

            # Install failed — clean up any partial directories before retry
            partial_dirs = dirs_after - original_dirs
            for d in partial_dirs:
                partial_path = os.path.join(comfy_path, "custom_nodes", d)
                print(f"  Cleaning up partial clone: {d}")
                shutil.rmtree(partial_path, ignore_errors=True)

            if result.returncode != 0:
                print(f"  ERROR (exit {result.returncode}): {result.stderr.strip()[-800:]}")
            else:
                print(f"  ERROR: comfy-cli returned success but created no directory")
                print(f"  stdout: {result.stdout.strip()[-800:]}")
                print(f"  stderr: {result.stderr.strip()[-800:]}")

            if attempt < MAX_INSTALL_RETRIES:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                print(f"  Retrying in {delay}s (attempt {attempt + 1}/{MAX_INSTALL_RETRIES})...")
                time.sleep(delay)
            else:
                failed.append({
                    "name": name,
                    "version": version,
                    "error": f"Failed after {MAX_INSTALL_RETRIES} attempts. stdout={result.stdout}, stderr={result.stderr}",
                })
                sys.exit(1)

        if not installed:
            # Should not reach here — sys.exit handles failure above
            sys.exit(1)

    return successful, failed


def verify_installations(config: dict, comfy_path: str) -> list[str]:
    """Verify that all expected custom nodes are installed."""
    custom_nodes_dir = os.path.join(comfy_path, "custom_nodes")
    missing = []

    if not os.path.exists(custom_nodes_dir):
        print(f"ERROR: custom_nodes directory not found at {custom_nodes_dir}")
        return [
            pack["name"]
            for pack in config.get("node_packs", [])
            if pack.get("name", "").lower() not in ["comfyui-manager", "comfyui_manager", "core"]
        ]

    installed_dirs = set()
    for item in os.listdir(custom_nodes_dir):
        item_path = os.path.join(custom_nodes_dir, item)
        if os.path.isdir(item_path) and not item.startswith("."):
            installed_dirs.add(item.lower())

    for node_pack in config.get("node_packs", []):
        name = node_pack.get("name", "")
        if name.lower() in ["comfyui-manager", "comfyui_manager", "core"]:
            continue

        expected_dir = name.lower()
        # Handle GitHub URL format
        if expected_dir.startswith("https://"):
            # Extract repo name from URL like https://github.com/user/repo@commit
            expected_dir = expected_dir.split("/")[-1].split("@")[0].lower()

        # Normalize hyphens to underscores — comfy-cli may use either
        # (e.g. registry ID "comfyui-bfsnodes" → dir "ComfyUI-BFSNodes"
        # or "comfyui_bfsnodes" depending on the source).
        norm_expected = expected_dir.replace("-", "_")
        found = any(
            norm_expected in installed_dir.replace("-", "_")
            or installed_dir.replace("-", "_") in norm_expected
            for installed_dir in installed_dirs
        )

        if found:
            print(f"  OK: {name}")
        else:
            print(f"  MISSING: {name}")
            missing.append(name)

    return missing


def main():
    parser = argparse.ArgumentParser(
        description="Install ComfyUI custom nodes for Comfy Complete"
    )
    parser.add_argument(
        "--comfy-path",
        type=str,
        default=os.environ.get("COMFY_PATH", "/app/comfyui"),
        help="Path to ComfyUI installation (default: $COMFY_PATH or /app/comfyui)",
    )
    parser.add_argument(
        "--config",
        type=str,
        help="Path to supported_nodes.yaml config file",
    )
    parser.add_argument(
        "--no-deps",
        action="store_true",
        help="Pass --no-deps flag to comfy node install (for production builds)",
    )
    parser.add_argument(
        "--no-uv",
        action="store_true",
        help="Use pip3 instead of uv for package installation (uv is used by default)",
    )
    args = parser.parse_args()

    # Determine pip command (uv by default)
    use_uv = not args.no_uv
    pip_cmd = ["uv", "pip"] if use_uv else ["pip3"]

    # Find comfy-cli
    comfy_cli_path = find_comfy_cli()
    if not comfy_cli_path:
        print("ERROR: comfy-cli not found. Install it with: pip install comfy-cli")
        sys.exit(1)
    print(f"Using comfy-cli at: {comfy_cli_path}")

    # Find and load config
    config_path = find_config_file(args.config)
    if not config_path:
        print("ERROR: supported_nodes.yaml not found")
        print("Searched locations:")
        print("  - Repository root (../supported_nodes.yaml)")
        print("  - Docker path (/app/supported_nodes.yaml)")
        print("  - Script directory")
        sys.exit(1)
    print(f"Using config: {config_path}")

    config = load_config(config_path)

    # Validate ComfyUI path
    if not os.path.exists(args.comfy_path):
        print(f"ERROR: ComfyUI path does not exist: {args.comfy_path}")
        sys.exit(1)
    print(f"ComfyUI path: {args.comfy_path}")

    # Change to ComfyUI directory for comfy-cli
    original_dir = os.getcwd()
    os.chdir(args.comfy_path)

    try:
        # Install ComfyUI Manager (required for comfy-cli node install)
        if not install_comfyui_manager(args.comfy_path, pip_cmd):
            sys.exit(1)

        # Create skip_download_model file to prevent model downloads during install
        skip_file = os.path.join(args.comfy_path, "custom_nodes", "skip_download_model")
        Path(skip_file).touch()

        # Install custom nodes
        print("\n=== Installing Custom Nodes ===")
        successful, failed = install_custom_nodes(
            config, comfy_cli_path, args.comfy_path, no_deps=args.no_deps
        )

        # Print summary
        print("\n=== Installation Summary ===")
        print(f"Successfully installed: {len(successful)} nodes")
        print(f"Failed installations: {len(failed)} nodes")

        if failed:
            print("\n=== Failed Installations ===")
            for failure in failed:
                print(f"  {failure['name']}@{failure['version']}")
                print(f"    Error: {failure['error']}")
            sys.exit(1)

        # Verify installations
        print("\n=== Verifying Installations ===")
        missing = verify_installations(config, args.comfy_path)

        if missing:
            print(f"\nERROR: {len(missing)} expected custom nodes are missing!")
            sys.exit(1)

        # List all installed custom nodes
        print("\n=== Installed Custom Node Directories ===")
        custom_nodes_dir = os.path.join(args.comfy_path, "custom_nodes")
        if os.path.exists(custom_nodes_dir):
            for item in sorted(os.listdir(custom_nodes_dir)):
                item_path = os.path.join(custom_nodes_dir, item)
                if os.path.isdir(item_path) and not item.startswith("."):
                    print(f"  {item}")

        print("\n=== Installation Complete ===")

    finally:
        os.chdir(original_dir)


if __name__ == "__main__":
    main()
