"""
Tests for resolve_disabled_nodes.py

Verifies the label-based node filtering logic works correctly.
"""

import sys
from pathlib import Path

import pytest
import yaml

# Add scripts directory to path so we can import the module
REPO_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from resolve_disabled_nodes import (
    get_all_disabled_nodes,
    get_node_labels,
    resolve_filter,
    validate_labels,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def simple_supported_nodes():
    """Minimal supported_nodes.yaml structure for testing."""
    return {
        "labels": [
            "ReadsArbitraryFile",
            "WritesToDisk",
            "NetworkAccess",
            "Incompatible",
            "RuntimeModelDownload",
        ],
        "node_packs": [
            {
                "name": "test-pack-a",
                "version": "1.0.0",
                "node_labels": {
                    "LoadFromPath": ["ReadsArbitraryFile"],
                    "SaveToFile": ["WritesToDisk"],
                    "FetchModel": ["NetworkAccess", "RuntimeModelDownload"],
                },
            },
            {
                "name": "test-pack-b",
                "version": "2.0.0",
                "node_labels": {
                    "BrokenLoader": ["Incompatible"],
                },
            },
        ],
    }


@pytest.fixture
def filter_reads_writes():
    """Filter config that disables ReadsArbitraryFile and WritesToDisk."""
    return {
        "or": [
            {"ReadsArbitraryFile": True},
            {"WritesToDisk": True},
        ]
    }


@pytest.fixture
def filter_all_labels():
    """Filter config that disables all labeled nodes."""
    return {
        "or": [
            {"ReadsArbitraryFile": True},
            {"WritesToDisk": True},
            {"NetworkAccess": True},
            {"Incompatible": True},
            {"RuntimeModelDownload": True},
        ]
    }


# ---------------------------------------------------------------------------
# Tests: get_node_labels
# ---------------------------------------------------------------------------

class TestGetNodeLabels:
    def test_basic_extraction(self, simple_supported_nodes):
        labels = get_node_labels(simple_supported_nodes)
        assert "LoadFromPath" in labels
        assert "ReadsArbitraryFile" in labels["LoadFromPath"]

    def test_multi_label_node(self, simple_supported_nodes):
        labels = get_node_labels(simple_supported_nodes)
        assert "FetchModel" in labels
        assert labels["FetchModel"] == {"NetworkAccess", "RuntimeModelDownload"}

    def test_empty_config(self):
        labels = get_node_labels({"node_packs": []})
        assert labels == {}

    def test_no_node_labels_key(self):
        config = {"node_packs": [{"name": "clean-pack", "version": "1.0"}]}
        labels = get_node_labels(config)
        assert labels == {}

    def test_node_across_packs(self):
        """If the same node name appears in multiple packs, labels merge."""
        config = {
            "node_packs": [
                {"name": "a", "node_labels": {"SharedNode": ["WritesToDisk"]}},
                {"name": "b", "node_labels": {"SharedNode": ["NetworkAccess"]}},
            ]
        }
        labels = get_node_labels(config)
        assert labels["SharedNode"] == {"WritesToDisk", "NetworkAccess"}


# ---------------------------------------------------------------------------
# Tests: resolve_filter
# ---------------------------------------------------------------------------

class TestResolveFilter:
    def test_or_filter_basic(self, simple_supported_nodes, filter_reads_writes):
        labels = get_node_labels(simple_supported_nodes)
        disabled = resolve_filter(labels, filter_reads_writes)
        assert "LoadFromPath" in disabled
        assert "SaveToFile" in disabled
        assert "FetchModel" not in disabled
        assert "BrokenLoader" not in disabled

    def test_or_filter_all(self, simple_supported_nodes, filter_all_labels):
        labels = get_node_labels(simple_supported_nodes)
        disabled = resolve_filter(labels, filter_all_labels)
        assert disabled == {"LoadFromPath", "SaveToFile", "FetchModel", "BrokenLoader"}

    def test_empty_filter(self, simple_supported_nodes):
        labels = get_node_labels(simple_supported_nodes)
        disabled = resolve_filter(labels, {})
        assert disabled == set()

    def test_empty_or_list(self, simple_supported_nodes):
        labels = get_node_labels(simple_supported_nodes)
        disabled = resolve_filter(labels, {"or": []})
        assert disabled == set()

    def test_filter_nonexistent_label(self, simple_supported_nodes):
        """Filter for a label that no node has should return empty."""
        labels = get_node_labels(simple_supported_nodes)
        disabled = resolve_filter(labels, {"or": [{"RequiresWebcam": True}]})
        assert disabled == set()

    def test_negative_filter(self):
        """Filter with False value disables nodes WITHOUT the label."""
        labels = {"NodeA": {"WritesToDisk"}, "NodeB": set()}
        disabled = resolve_filter(labels, {"or": [{"WritesToDisk": False}]})
        assert "NodeB" in disabled
        assert "NodeA" not in disabled


# ---------------------------------------------------------------------------
# Tests: get_all_disabled_nodes
# ---------------------------------------------------------------------------

class TestGetAllDisabledNodes:
    def test_combines_static_and_dynamic(self):
        config = {
            "node_packs": [
                {
                    "name": "test",
                    "disallow_nodes": ["StaticDisabled"],
                    "node_labels": {
                        "DynamicDisabled": ["Incompatible"],
                    },
                }
            ]
        }
        filter_config = {"or": [{"Incompatible": True}]}
        disabled = get_all_disabled_nodes(config, filter_config)
        assert "StaticDisabled" in disabled
        assert "DynamicDisabled" in disabled

    def test_no_filter_config(self):
        config = {
            "node_packs": [
                {
                    "name": "test",
                    "disallow_nodes": ["StaticOnly"],
                    "node_labels": {"Labeled": ["WritesToDisk"]},
                }
            ]
        }
        disabled = get_all_disabled_nodes(config, None)
        assert "StaticOnly" in disabled
        assert "Labeled" not in disabled

    def test_sorted_output(self, simple_supported_nodes, filter_all_labels):
        disabled = get_all_disabled_nodes(simple_supported_nodes, filter_all_labels)
        assert disabled == sorted(disabled)


# ---------------------------------------------------------------------------
# Tests: validate_labels
# ---------------------------------------------------------------------------

class TestValidateLabels:
    def test_valid_labels(self, simple_supported_nodes):
        errors = validate_labels(simple_supported_nodes)
        assert errors == []

    def test_undeclared_label(self):
        config = {
            "labels": ["WritesToDisk"],
            "node_packs": [
                {
                    "name": "test",
                    "node_labels": {"BadNode": ["UndeclaredLabel"]},
                }
            ],
        }
        errors = validate_labels(config)
        assert len(errors) == 1
        assert "UndeclaredLabel" in errors[0]

    def test_no_labels_declaration(self):
        config = {
            "node_packs": [
                {"name": "test", "node_labels": {"Node": ["WritesToDisk"]}}
            ]
        }
        errors = validate_labels(config)
        assert len(errors) == 1

    def test_empty_config(self):
        errors = validate_labels({"labels": [], "node_packs": []})
        assert errors == []


# ---------------------------------------------------------------------------
# Tests: Integration with real supported_nodes.yaml
# ---------------------------------------------------------------------------

class TestRealConfig:
    """Tests that run against the actual supported_nodes.yaml."""

    @pytest.fixture
    def real_config(self):
        yaml_file = REPO_ROOT / "supported_nodes.yaml"
        if not yaml_file.exists():
            pytest.skip("supported_nodes.yaml not found")
        with open(yaml_file) as f:
            return yaml.safe_load(f)

    def test_all_labels_valid(self, real_config):
        """Every label used in supported_nodes.yaml must be declared."""
        errors = validate_labels(real_config)
        assert errors == [], f"Label validation errors:\n" + "\n".join(errors)

    def test_labels_count(self, real_config):
        """Verify the declared label count matches the current set.

        Current state (post cloud->external migration): 10 labels, matching
        the production cloud content as of 2026-05. The PRD-v2 expansion to
        18 labels (adding Incompatible, RequiresGPU, BrokenNode, etc.) is
        Milestone B work — when that migration ships, bump this assertion
        and update test_expected_labels_present below.
        """
        assert len(real_config.get("labels", [])) == 10

    def test_expected_labels_present(self, real_config):
        """Verify all expected labels are declared.

        Current set reflects production cloud content. PRD-v2 expanded set
        is Milestone B work; see test_labels_count docstring.
        """
        expected = {
            "ReadsArbitraryFile", "CreatesLargeOutputs", "DisabledOnCloud",
            "WritesToDisk", "NetworkAccess", "Stateful",
            "HasCustomEndpoints", "RequiresExternalAPI", "PathParsing",
            "DuplicateOfCoreNode",
        }
        declared = set(real_config.get("labels", []))
        assert declared == expected

    def test_no_empty_label_lists(self, real_config):
        """No node should have an empty label list."""
        for pack in real_config.get("node_packs", []):
            pack_name = pack.get("name", "unknown")
            for node_name, labels in pack.get("node_labels", {}).items():
                assert len(labels) > 0, (
                    f"Node '{node_name}' in '{pack_name}' has empty label list"
                )

    def test_no_duplicate_labels_per_node(self, real_config):
        """No node should have duplicate labels."""
        for pack in real_config.get("node_packs", []):
            pack_name = pack.get("name", "unknown")
            for node_name, labels in pack.get("node_labels", {}).items():
                assert len(labels) == len(set(labels)), (
                    f"Node '{node_name}' in '{pack_name}' has duplicate labels: {labels}"
                )

    def test_disabled_node_count_with_cloud_config(self, real_config):
        """Verify disabled node count with cloud config is reasonable."""
        cloud_config_path = REPO_ROOT.parent / "cloud_disable_config.yaml"
        if not cloud_config_path.exists():
            pytest.skip("cloud_disable_config.yaml not found")

        with open(cloud_config_path) as f:
            cloud_config = yaml.safe_load(f)

        filter_config = cloud_config.get("disable_nodes", {})
        disabled = get_all_disabled_nodes(real_config, filter_config)
        # Should be a substantial number (400+) given how many labels we have
        assert len(disabled) > 200, (
            f"Expected 200+ disabled nodes, got {len(disabled)}"
        )

    def test_every_pack_has_name(self, real_config):
        """Every node pack must have a name."""
        for i, pack in enumerate(real_config.get("node_packs", [])):
            assert "name" in pack, f"Node pack at index {i} missing 'name'"
            assert pack["name"], f"Node pack at index {i} has empty name"
