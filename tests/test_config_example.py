"""
Tests for config.yaml.example

Verifies the example configuration file is valid and uses only declared labels.
"""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent


def test_config_example_valid_yaml():
    """config.yaml.example must be valid YAML."""
    config_file = REPO_ROOT / "config.yaml.example"
    if not config_file.exists():
        pytest.skip("config.yaml.example not found")

    with open(config_file) as f:
        config = yaml.safe_load(f)

    assert config is not None, "config.yaml.example is empty"
    assert isinstance(config, dict), "config.yaml.example must be a YAML mapping"


def test_config_example_disable_nodes_structure():
    """disable_nodes section must use valid OR-filter structure."""
    config_file = REPO_ROOT / "config.yaml.example"
    if not config_file.exists():
        pytest.skip("config.yaml.example not found")

    with open(config_file) as f:
        config = yaml.safe_load(f)

    if "disable_nodes" not in config:
        return  # disable_nodes is optional

    dn = config["disable_nodes"]
    assert isinstance(dn, dict), "disable_nodes must be a mapping"
    assert "or" in dn, "disable_nodes must have an 'or' key"
    assert isinstance(dn["or"], list), "disable_nodes.or must be a list"

    for i, condition in enumerate(dn["or"]):
        assert isinstance(condition, dict), (
            f"disable_nodes.or[{i}] must be a mapping"
        )
        for label, value in condition.items():
            assert isinstance(value, bool), (
                f"disable_nodes.or[{i}].{label} must be a boolean, got {type(value)}"
            )


def test_config_example_labels_are_declared():
    """Labels used in config.yaml.example must be declared in supported_nodes.yaml."""
    config_file = REPO_ROOT / "config.yaml.example"
    nodes_file = REPO_ROOT / "supported_nodes.yaml"

    if not config_file.exists():
        pytest.skip("config.yaml.example not found")
    if not nodes_file.exists():
        pytest.skip("supported_nodes.yaml not found")

    with open(config_file) as f:
        config = yaml.safe_load(f)
    with open(nodes_file) as f:
        nodes_config = yaml.safe_load(f)

    declared_labels = set(nodes_config.get("labels", []))

    if "disable_nodes" not in config:
        return

    for condition in config["disable_nodes"].get("or", []):
        for label in condition:
            assert label in declared_labels, (
                f"Label '{label}' in config.yaml.example is not declared "
                f"in supported_nodes.yaml labels list"
            )
