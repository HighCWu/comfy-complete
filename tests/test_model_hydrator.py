"""Unit tests for the short-lived shared Network Volume hydrator."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "model_hydrator", ROOT / "docker" / "hydrator" / "model_hydrator.py"
)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class FakeResponse(io.BytesIO):
    status = 200

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def manifest(payload: bytes) -> dict[str, object]:
    sha256 = hashlib.sha256(payload).hexdigest()
    path = f"shared/models/objects/{sha256[:2]}/{sha256}/artifact"
    return {
        "sha256": sha256,
        "size_bytes": len(payload),
        "artifact_path": path,
        "marker_path": path + ".manifest.json",
        "reserve_bytes": 0,
        "overhead_bps": 0,
        "source_kind": "huggingface",
        "download_url": "https://huggingface.co/owner/repo/resolve/main/model.bin",
    }


class ModelHydratorTests(unittest.TestCase):
    def test_hydration_integration_is_optional(self) -> None:
        with patch.dict(module.os.environ, {}, clear=True):
            self.assertFalse(module.integration_configured())

    def test_hydration_integration_rejects_partial_configuration(self) -> None:
        with patch.dict(
            module.os.environ,
            {"COMFY_CONTROL_PLANE_URL": "https://example.invalid"},
            clear=True,
        ):
            with self.assertRaisesRegex(module.HydrationError, "configured together"):
                module.integration_configured()

    def test_rejects_path_not_bound_to_hash(self) -> None:
        value = {
            "sha256": "a" * 64,
            "size_bytes": 10,
            "artifact_path": "shared/models/objects/bb/" + "b" * 64 + "/artifact",
            "marker_path": "shared/models/objects/bb/" + "b" * 64 + "/artifact.manifest.json",
            "reserve_bytes": 0,
            "overhead_bps": 0,
            "source": {
                "kind": "huggingface",
                "download_url": "https://huggingface.co/owner/repo/resolve/main/model.bin",
            },
        }
        with self.assertRaisesRegex(module.HydrationError, "metadata is invalid"):
            module.validate_manifest(value)

    def test_publishes_artifact_then_immutable_marker(self) -> None:
        payload = b"small-public-model"
        with tempfile.TemporaryDirectory() as directory, patch.object(
            module.urllib.request.OpenerDirector,
            "open",
            return_value=FakeResponse(payload),
        ):
            root = Path(directory)
            result = module.hydrate(root, manifest(payload))
            relative = str(manifest(payload)["artifact_path"])
            artifact = root / relative
            marker = root / (relative + ".manifest.json")
            self.assertEqual(result["status"], "ready")
            self.assertEqual(artifact.read_bytes(), payload)
            self.assertEqual(
                json.loads(marker.read_text(encoding="utf-8")),
                {
                    "version": 1,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size_bytes": len(payload),
                    "artifact_path": relative,
                },
            )

    def test_reuses_only_a_matching_published_artifact(self) -> None:
        payload = b"already-cached"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_manifest = manifest(payload)
            relative = str(first_manifest["artifact_path"])
            artifact = root / relative
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(payload)
            (root / (relative + ".manifest.json")).write_text(
                json.dumps({
                    "version": 1,
                    "sha256": first_manifest["sha256"],
                    "size_bytes": len(payload),
                    "artifact_path": relative,
                }),
                encoding="utf-8",
            )
            with patch.object(module.urllib.request.OpenerDirector, "open") as request:
                result = module.hydrate(root, first_manifest)
            request.assert_not_called()
            self.assertEqual(result["status"], "already_ready")

    def test_refuses_download_when_reserved_headroom_would_be_crossed(self) -> None:
        payload = b"capacity-guard"
        value = manifest(payload)
        value["reserve_bytes"] = 100
        with tempfile.TemporaryDirectory() as directory, patch.object(
            module,
            "capacity",
            return_value={"total_bytes": 1_000, "free_bytes": 50},
        ), patch.object(module.urllib.request.OpenerDirector, "open") as request:
            with self.assertRaisesRegex(module.HydrationError, "required"):
                module.hydrate(Path(directory), value)
            request.assert_not_called()

    def test_rejects_huggingface_redirect_to_an_untrusted_host(self) -> None:
        handler = module.TrustedRedirectHandler()
        request = module.urllib.request.Request(
            "https://huggingface.co/owner/repo/resolve/main/model.bin"
        )
        with self.assertRaisesRegex(module.HydrationError, "untrusted"):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://attacker.example/model.bin",
            )


if __name__ == "__main__":
    unittest.main()
