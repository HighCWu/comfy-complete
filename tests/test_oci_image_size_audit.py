"""Tests for the remote OCI compressed-size deployment guard."""

import importlib.util
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "audit_oci_image_size.py"
SPEC = importlib.util.spec_from_file_location("audit_oci_image_size", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def manifest(*sizes: int) -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "layers": [
            {"size": size, "digest": f"sha256:{index:064x}"}
            for index, size in enumerate(sizes, start=1)
        ],
    }


def test_reports_total_largest_and_large_layer_count() -> None:
    report = module.inspect_manifest(
        manifest(module.GIB * 2, module.GIB // 2, module.GIB)
    )

    assert report["layerCount"] == 3
    assert report["compressedBytes"] == module.GIB * 3 + module.GIB // 2
    assert report["compressedGiB"] == 3.5
    assert report["largestLayerGiB"] == 2.0
    assert report["layersAtLeastOneGiB"] == 2
    assert [item["index"] for item in report["largestLayers"]] == [0, 2, 1]


@pytest.mark.parametrize(
    "value, message",
    [
        ({}, "non-empty layers"),
        ({"layers": []}, "non-empty layers"),
        ({"layers": [None]}, "not an object"),
        ({"layers": [{"size": True, "digest": "sha256:x"}]}, "compressed size"),
        ({"layers": [{"size": 1, "digest": "latest"}]}, "invalid digest"),
    ],
)
def test_rejects_malformed_or_multi_platform_manifest(
    value: dict[str, object], message: str
) -> None:
    with pytest.raises(module.ImageSizeAuditError, match=message):
        module.inspect_manifest(value)
