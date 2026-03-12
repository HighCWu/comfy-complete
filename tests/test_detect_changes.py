"""
Tests for detect_changes.py

Verifies the PR change detection logic for supported_nodes.yaml modifications.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts" / "pr-review"
sys.path.insert(0, str(SCRIPTS_DIR))

from detect_changes import (
    ChangeReport,
    LabelChanges,
    NodePack,
    detect_label_changes,
)


# ---------------------------------------------------------------------------
# Tests: NodePack dataclass
# ---------------------------------------------------------------------------

class TestNodePack:
    def test_from_dict_minimal(self):
        pack = NodePack.from_dict({"name": "test-pack"})
        assert pack.name == "test-pack"
        assert pack.version == ""
        assert pack.node_labels == {}
        assert pack.dependency_overrides == []
        assert pack.system_dependencies == []
        assert pack.models == []

    def test_from_dict_full(self):
        data = {
            "name": "full-pack",
            "version": "1.2.3",
            "node_labels": {"NodeA": ["WritesToDisk"]},
            "web_directory": "js",
            "dependency_overrides": ["torch>=2.0"],
            "system_dependencies": ["ffmpeg"],
            "models": [{"name": "model.safetensors", "url": "https://example.com"}],
        }
        pack = NodePack.from_dict(data)
        assert pack.name == "full-pack"
        assert pack.version == "1.2.3"
        assert pack.node_labels == {"NodeA": ["WritesToDisk"]}
        assert pack.web_directory == "js"
        assert pack.dependency_overrides == ["torch>=2.0"]
        assert pack.system_dependencies == ["ffmpeg"]
        assert pack.models == [{"name": "model.safetensors", "url": "https://example.com"}]

    def test_to_dict_minimal(self):
        pack = NodePack(name="test")
        d = pack.to_dict()
        assert d["name"] == "test"
        assert "dependency_overrides" not in d  # empty lists omitted
        assert "system_dependencies" not in d
        assert "models" not in d

    def test_to_dict_includes_nonempty_new_fields(self):
        pack = NodePack(
            name="test",
            dependency_overrides=["torch>=2.0"],
            models=[{"name": "m.pt"}],
        )
        d = pack.to_dict()
        assert d["dependency_overrides"] == ["torch>=2.0"]
        assert d["models"] == [{"name": "m.pt"}]
        assert "system_dependencies" not in d

    def test_roundtrip(self):
        data = {
            "name": "roundtrip",
            "version": "1.0",
            "node_labels": {"A": ["WritesToDisk", "NetworkAccess"]},
            "web_directory": "",
            "dependency_overrides": ["dep1"],
            "system_dependencies": [],
            "models": [],
        }
        pack = NodePack.from_dict(data)
        result = pack.to_dict()
        assert result["name"] == "roundtrip"
        assert result["version"] == "1.0"
        assert result["dependency_overrides"] == ["dep1"]


# ---------------------------------------------------------------------------
# Tests: detect_label_changes
# ---------------------------------------------------------------------------

class TestDetectLabelChanges:
    def test_no_changes(self):
        labels = {"NodeA": ["WritesToDisk"]}
        changes = detect_label_changes(labels, labels)
        assert changes.added == {}
        assert changes.removed == {}
        assert changes.modified == {}

    def test_added_node(self):
        base = {}
        head = {"NewNode": ["NetworkAccess"]}
        changes = detect_label_changes(base, head)
        assert "NewNode" in changes.added
        assert changes.added["NewNode"] == ["NetworkAccess"]

    def test_removed_node(self):
        base = {"OldNode": ["WritesToDisk"]}
        head = {}
        changes = detect_label_changes(base, head)
        assert "OldNode" in changes.removed

    def test_modified_labels(self):
        base = {"Node": ["WritesToDisk"]}
        head = {"Node": ["WritesToDisk", "NetworkAccess"]}
        changes = detect_label_changes(base, head)
        assert "Node" in changes.modified
        assert changes.modified["Node"]["old"] == ["WritesToDisk"]
        assert changes.modified["Node"]["new"] == ["WritesToDisk", "NetworkAccess"]


# ---------------------------------------------------------------------------
# Tests: ChangeReport
# ---------------------------------------------------------------------------

class TestChangeReport:
    def test_no_changes(self):
        report = ChangeReport()
        assert not report.has_changes
        d = report.to_dict()
        assert d["summary"]["has_changes"] is False

    def test_with_new_pack(self):
        report = ChangeReport(new=[NodePack(name="new-pack", version="1.0")])
        assert report.has_changes
        d = report.to_dict()
        assert d["summary"]["new_count"] == 1
        assert d["new"][0]["name"] == "new-pack"

    def test_with_removed_pack(self):
        report = ChangeReport(removed=[NodePack(name="old-pack")])
        assert report.has_changes
        d = report.to_dict()
        assert d["summary"]["removed_count"] == 1

    def test_with_updated_pack(self):
        report = ChangeReport(updated=[{"name": "pack", "version_changed": True}])
        assert report.has_changes
        d = report.to_dict()
        assert d["summary"]["updated_count"] == 1


# ---------------------------------------------------------------------------
# Tests: detect_changes preserves dependency_overrides on updates
# ---------------------------------------------------------------------------

class TestDetectChangesUpdatedPack:
    """Test that updated packs preserve all fields including dependency_overrides."""

    def test_updated_pack_preserves_dependency_overrides(self, tmp_path, monkeypatch):
        """When a pack is updated, dependency_overrides must appear in update_info."""
        from detect_changes import detect_changes

        base_yaml = {
            "node_packs": [
                {
                    "name": "test-pack",
                    "version": "1.0",
                    "node_labels": {},
                    "dependency_overrides": ["torch>=2.0"],
                },
            ],
        }
        head_yaml = {
            "node_packs": [
                {
                    "name": "test-pack",
                    "version": "2.0",
                    "node_labels": {},
                    "dependency_overrides": ["torch>=2.1", "numpy>=1.24"],
                },
            ],
        }

        import yaml as _yaml

        config_file = tmp_path / "supported_nodes.yaml"
        config_file.write_text(_yaml.dump(head_yaml))

        # Stub load_yaml_from_git to return base_yaml without touching git
        import detect_changes as dc

        monkeypatch.setattr(dc, "load_yaml_from_git", lambda _ref, _path: base_yaml)

        report = detect_changes("fake-ref", str(config_file))

        assert len(report.updated) == 1
        update = report.updated[0]
        assert update["name"] == "test-pack"
        assert update["base"]["dependency_overrides"] == ["torch>=2.0"]
        assert update["head"]["dependency_overrides"] == ["torch>=2.1", "numpy>=1.24"]
        assert update.get("version_changed") is True

    def test_updated_pack_omits_empty_dependency_overrides(self, tmp_path, monkeypatch):
        """dependency_overrides should be omitted when empty (consistent with to_dict)."""
        from detect_changes import detect_changes

        base_yaml = {
            "node_packs": [
                {"name": "test-pack", "version": "1.0", "node_labels": {}},
            ],
        }
        head_yaml = {
            "node_packs": [
                {"name": "test-pack", "version": "2.0", "node_labels": {}},
            ],
        }

        import yaml as _yaml

        config_file = tmp_path / "supported_nodes.yaml"
        config_file.write_text(_yaml.dump(head_yaml))

        import detect_changes as dc

        monkeypatch.setattr(dc, "load_yaml_from_git", lambda _ref, _path: base_yaml)

        report = detect_changes("fake-ref", str(config_file))

        assert len(report.updated) == 1
        update = report.updated[0]
        assert "dependency_overrides" not in update["base"]
        assert "dependency_overrides" not in update["head"]
