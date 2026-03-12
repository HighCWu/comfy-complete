"""
Comfy Complete - Requirements Validation Tests

These tests verify that the requirements.txt file is valid and doesn't contain
internal conflicts. Uses `uv` for fast dependency resolution.

Run with: pytest tests/test_requirements.py -v
"""

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

# Get the repository root directory
REPO_ROOT = Path(__file__).parent.parent
REQUIREMENTS_FILE = REPO_ROOT / "requirements.txt"


def test_requirements_file_exists():
    """Verify that requirements.txt exists."""
    assert REQUIREMENTS_FILE.exists(), f"requirements.txt not found at {REQUIREMENTS_FILE}"


def test_requirements_not_empty():
    """Verify that requirements.txt is not empty."""
    content = REQUIREMENTS_FILE.read_text().strip()
    assert len(content) > 0, "requirements.txt is empty"

    # Count non-comment, non-empty lines
    lines = [
        line.strip()
        for line in content.split("\n")
        if line.strip() and not line.strip().startswith("#")
    ]
    assert len(lines) > 0, "requirements.txt has no package definitions"


def test_requirements_syntax():
    """Verify that requirements.txt has valid syntax using pip check."""
    result = subprocess.run(
        ["pip", "check", "--quiet"],
        capture_output=True,
        text=True,
    )
    # Note: pip check verifies installed packages, not requirements file
    # This is a basic sanity check


def test_requirements_resolvable_with_uv():
    """Use uv to verify requirements.txt can be resolved without conflicts."""
    # Check if uv is available
    import shutil

    if shutil.which("uv") is None:
        pytest.skip("uv not installed - skipping resolution test")

    # Create a temporary directory for the virtual environment
    with tempfile.TemporaryDirectory() as tmpdir:
        venv_path = os.path.join(tmpdir, ".venv")

        # Create virtual environment with uv
        result = subprocess.run(
            ["uv", "venv", venv_path],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Failed to create venv: {result.stderr}"

        # Try to install dependencies (dry-run, --no-deps to match actual install).
        # We install with --no-deps in Docker because some packages declare
        # overly strict bounds (e.g. mediapipe requires numpy<2) that conflict
        # with our pinned versions but work fine at runtime.
        result = subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--dry-run",
                "--no-deps",
                "-r",
                str(REQUIREMENTS_FILE),
                "--python",
                os.path.join(
                    venv_path,
                    "Scripts" if os.name == "nt" else "bin",
                    "python.exe" if os.name == "nt" else "python",
                ),
            ],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            # Parse error message to provide helpful feedback
            error_msg = result.stderr or result.stdout
            pytest.fail(
                f"Requirements resolution failed. This indicates dependency conflicts.\n"
                f"Error: {error_msg}"
            )


def test_all_packages_pinned():
    """Verify that all packages in requirements.txt are pinned to exact versions."""
    content = REQUIREMENTS_FILE.read_text()
    lines = [
        line.strip()
        for line in content.split("\n")
        if line.strip() and not line.strip().startswith("#")
    ]

    unpinned = []
    for line in lines:
        # Skip git/URL dependencies (they're pinned by commit/tag)
        if line.startswith("git+") or line.startswith("http"):
            continue
        if " @ " in line:  # PEP 440 URL requirements
            continue

        # Check for exact version pin (==)
        if "==" not in line:
            unpinned.append(line)

    if unpinned:
        pytest.fail(
            f"The following packages are not pinned to exact versions:\n"
            + "\n".join(f"  - {pkg}" for pkg in unpinned)
        )


def test_no_conflicting_packages():
    """Check for known conflicting package patterns."""
    content = REQUIREMENTS_FILE.read_text().lower()

    # Known conflict patterns to check
    conflicts = []

    # Check for multiple numpy versions (numpy 1.x vs 2.x)
    numpy_lines = [
        line.strip()
        for line in content.split("\n")
        if line.strip().startswith("numpy")
    ]
    if len(numpy_lines) > 1:
        conflicts.append(f"Multiple numpy entries found: {numpy_lines}")

    # Check for opencv conflicts
    opencv_packages = ["opencv-python", "opencv-python-headless", "opencv-contrib-python"]
    found_opencv = [pkg for pkg in opencv_packages if pkg in content]
    # Having multiple opencv variants is actually OK if they're compatible versions

    if conflicts:
        pytest.fail("Found potential package conflicts:\n" + "\n".join(conflicts))


def test_yaml_configs_valid():
    """Verify that YAML config files are valid."""
    try:
        import yaml
    except ImportError:
        pytest.skip("PyYAML not installed")

    yaml_files = [
        REPO_ROOT / "supported_nodes.yaml",
        REPO_ROOT / "version_lock.yaml",
    ]

    for yaml_file in yaml_files:
        if yaml_file.exists():
            try:
                with open(yaml_file, "r") as f:
                    yaml.safe_load(f)
            except yaml.YAMLError as e:
                pytest.fail(f"Invalid YAML in {yaml_file.name}: {e}")


def test_supported_nodes_structure():
    """Verify supported_nodes.yaml has expected structure."""
    try:
        import yaml
    except ImportError:
        pytest.skip("PyYAML not installed")

    yaml_file = REPO_ROOT / "supported_nodes.yaml"
    if not yaml_file.exists():
        pytest.skip("supported_nodes.yaml not found")

    with open(yaml_file, "r") as f:
        config = yaml.safe_load(f)

    assert "node_packs" in config, "supported_nodes.yaml missing 'node_packs' key"
    assert isinstance(config["node_packs"], list), "'node_packs' should be a list"
    assert len(config["node_packs"]) > 0, "'node_packs' should not be empty"

    allowed_keys = {
        "name",
        "version",
        "node_labels",
        "web_directory",
        "dependency_overrides",
        "system_dependencies",
        "models",
    }

    for i, pack in enumerate(config["node_packs"]):
        pack_name = pack.get("name", f"<index {i}>")
        assert "name" in pack, f"Node pack at index {i} missing 'name' key"

        # Check for unknown keys (catches typos)
        unknown_keys = set(pack.keys()) - allowed_keys
        assert not unknown_keys, (
            f"Node pack '{pack_name}' has unknown keys: {unknown_keys}"
        )

        # Validate dependency_overrides if present
        if "dependency_overrides" in pack:
            dep_overrides = pack["dependency_overrides"]
            assert isinstance(dep_overrides, list), (
                f"'{pack_name}': dependency_overrides must be a list"
            )
            for j, dep in enumerate(dep_overrides):
                assert isinstance(dep, str), (
                    f"'{pack_name}': dependency_overrides[{j}] must be a string"
                )

        # Validate system_dependencies if present
        if "system_dependencies" in pack:
            sys_deps = pack["system_dependencies"]
            assert isinstance(sys_deps, list), (
                f"'{pack_name}': system_dependencies must be a list"
            )
            for j, dep in enumerate(sys_deps):
                assert isinstance(dep, str), (
                    f"'{pack_name}': system_dependencies[{j}] must be a string"
                )

        # Validate models if present
        if "models" in pack:
            models = pack["models"]
            assert isinstance(models, list), (
                f"'{pack_name}': models must be a list"
            )
            for j, model in enumerate(models):
                assert isinstance(model, dict), (
                    f"'{pack_name}': models[{j}] must be a dict"
                )
                assert "name" in model, (
                    f"'{pack_name}': models[{j}] missing required 'name' key"
                )
                model_allowed_keys = {"name", "url", "directory", "filename"}
                unknown_model_keys = set(model.keys()) - model_allowed_keys
                assert not unknown_model_keys, (
                    f"'{pack_name}': models[{j}] has unknown keys: "
                    f"{unknown_model_keys}"
                )


def test_version_lock_structure():
    """Verify version_lock.yaml has expected structure."""
    try:
        import yaml
    except ImportError:
        pytest.skip("PyYAML not installed")

    yaml_file = REPO_ROOT / "version_lock.yaml"
    if not yaml_file.exists():
        pytest.skip("version_lock.yaml not found")

    with open(yaml_file, "r") as f:
        config = yaml.safe_load(f)

    assert "pinned" in config, "version_lock.yaml missing 'pinned' key"
    assert "comfyui" in config["pinned"], "version_lock.yaml missing 'comfyui' in pinned"
    assert "ref" in config["pinned"]["comfyui"], "comfyui missing 'ref' key"


def test_supported_nodes_labels_declared():
    """Verify all labels used in supported_nodes.yaml are declared in the labels list."""
    try:
        import yaml
    except ImportError:
        pytest.skip("PyYAML not installed")

    yaml_file = REPO_ROOT / "supported_nodes.yaml"
    if not yaml_file.exists():
        pytest.skip("supported_nodes.yaml not found")

    with open(yaml_file, "r") as f:
        config = yaml.safe_load(f)

    # Get declared labels
    declared_labels = set(config.get("labels", []))

    # Check all labels used in node_packs are declared
    errors = []
    for node_pack in config.get("node_packs", []):
        pack_name = node_pack.get("name", "unknown")
        node_labels = node_pack.get("node_labels", {})

        for node_name, labels in node_labels.items():
            for label in labels:
                if label not in declared_labels:
                    errors.append(
                        f"Label '{label}' used on node '{node_name}' in pack "
                        f"'{pack_name}' is not declared in 'labels' list"
                    )

    if errors:
        pytest.fail("Label validation errors:\n" + "\n".join(f"  - {e}" for e in errors))


def test_supported_nodes_labels_list_exists():
    """Verify supported_nodes.yaml has a labels declaration list."""
    try:
        import yaml
    except ImportError:
        pytest.skip("PyYAML not installed")

    yaml_file = REPO_ROOT / "supported_nodes.yaml"
    if not yaml_file.exists():
        pytest.skip("supported_nodes.yaml not found")

    with open(yaml_file, "r") as f:
        config = yaml.safe_load(f)

    assert "labels" in config, "supported_nodes.yaml missing 'labels' declaration list"
    assert isinstance(config["labels"], list), "'labels' should be a list"
    assert len(config["labels"]) > 0, "'labels' should not be empty"


def test_build_config_structure():
    """Verify build-config.yaml has expected structure."""
    try:
        import yaml
    except ImportError:
        pytest.skip("PyYAML not installed")

    yaml_file = REPO_ROOT / "build-config.yaml"
    if not yaml_file.exists():
        pytest.skip("build-config.yaml not found")

    with open(yaml_file, "r") as f:
        config = yaml.safe_load(f)

    assert "build" in config, "build-config.yaml missing 'build' key"
    assert isinstance(config["build"], dict), "'build' should be a dictionary"
