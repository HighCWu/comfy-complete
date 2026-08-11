"""Unit tests for the Pod's lease-bound asset mirror client."""

from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pod_asset_sync", ROOT / "docker" / "pod" / "asset_sync.py"
)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class FakeResponse(io.BytesIO):
    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class PodAssetSyncTests(unittest.TestCase):
    def test_rejects_paths_that_escape_the_instance_root(self) -> None:
        for value in ("../secret.png", "/absolute.png", "a\\b.png", "a//b.png"):
            with self.subTest(value=value):
                with self.assertRaises(module.AssetSyncError):
                    module.safe_relative_path(value)

    def test_restores_verified_inputs_atomically(self) -> None:
        payload = b"durable-input"
        digest = module.hashlib.sha256(payload).hexdigest()
        manifest = {
            "version": 1,
            "instance_id": "inst_test",
            "items": [{
                "relative_path": "references/source.png",
                "sha256": digest,
                "size_bytes": len(payload),
                "download_path": "/api/internal/pod-assets/asset_1?instance_id=inst_test",
            }],
        }
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            module.os.environ,
            {
                "LAIMON_CONTROL_PLANE_URL": "https://laimon.ai",
                "LAIMON_INSTANCE_ID": "inst_test",
                "LAIMON_POD_TOKEN": "token",
            },
            clear=False,
        ), patch.object(module, "json_request", return_value=manifest), patch.object(
            module,
            "request",
            return_value=FakeResponse(payload),
        ):
            root = Path(directory)
            module.restore_inputs(root)
            target = root / "input" / "references" / "source.png"
            self.assertEqual(target.read_bytes(), payload)
            self.assertFalse(target.with_name("source.png.partial").exists())

    def test_multipart_upload_uses_bounded_parts_and_records_final_state(self) -> None:
        payload = b"x" * (module.PART_BYTES + 7)
        responses = iter([
            {"status": "uploading", "upload_id": "paup_test"},
            {"status": "ready", "asset_id": "past_test"},
        ])
        part_sizes: list[int] = []

        def fake_json_request(*_args: object, **_kwargs: object) -> object:
            return next(responses)

        def fake_request(
            _url: str,
            _token: str,
            *,
            data: bytes | None = None,
            **_kwargs: object,
        ) -> FakeResponse:
            assert data is not None
            part_sizes.append(len(data))
            response = {
                "part_number": len(part_sizes),
                "etag": f"etag-{len(part_sizes)}",
            }
            return FakeResponse(json.dumps(response).encode())

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            module.os.environ,
            {
                "LAIMON_CONTROL_PLANE_URL": "https://laimon.ai",
                "LAIMON_INSTANCE_ID": "inst_test",
                "LAIMON_POD_TOKEN": "token",
            },
            clear=False,
        ), patch.object(module, "json_request", side_effect=fake_json_request), patch.object(
            module,
            "request",
            side_effect=fake_request,
        ):
            root = Path(directory)
            output = root / "output" / "clip.bin"
            output.parent.mkdir(parents=True)
            output.write_bytes(payload)
            state = module.upload_file(root, "output", output)

        self.assertEqual(part_sizes, [module.PART_BYTES, 7])
        self.assertEqual(state["size"], len(payload))
        self.assertEqual(state["sha256"], module.hashlib.sha256(payload).hexdigest())


if __name__ == "__main__":
    unittest.main()
