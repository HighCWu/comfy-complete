"""Tests for the fail-closed, provider-neutral runtime object publisher."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from botocore.exceptions import ClientError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from runtime_manifest import canonical_json  # noqa: E402

SPEC = importlib.util.spec_from_file_location("publish_runtime", ROOT / "scripts" / "publish_runtime.py")
assert SPEC and SPEC.loader
publisher = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = publisher
SPEC.loader.exec_module(publisher)


MIB = 1024 * 1024


def not_found(key: str) -> ClientError:
    return ClientError({"Error": {"Code": "404", "Message": "missing"}}, "HeadObject")


class FakeObjectStore:
    """Small in-memory S3 surface that records mutation ordering."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.uploads: dict[str, dict[int, bytes]] = {}
        self.calls: list[str] = []
        self.next_upload = 1
        self.fail_part: int | None = None

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        del Bucket
        self.calls.append(f"head:{Key}")
        value = self.objects.get(Key)
        if value is None:
            raise not_found(Key)
        body, metadata = value
        return {"ContentLength": len(body), "Metadata": metadata.copy()}

    def create_multipart_upload(
        self,
        *,
        Bucket: str,
        Key: str,
        ContentType: str,
        Metadata: dict[str, str],
        CacheControl: str,
    ) -> dict[str, object]:
        del Bucket, ContentType, Metadata, CacheControl
        upload_id = f"upload-{self.next_upload}"
        self.next_upload += 1
        self.uploads[upload_id] = {}
        self.calls.append(f"create:{Key}")
        return {"UploadId": upload_id}

    def upload_part(
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
        PartNumber: int,
        Body: bytes,
    ) -> dict[str, object]:
        del Bucket
        self.calls.append(f"part:{Key}:{PartNumber}")
        if self.fail_part == PartNumber:
            raise RuntimeError("injected part failure")
        self.uploads[UploadId][PartNumber] = Body
        return {"ETag": f'"etag-{PartNumber}"'}

    def complete_multipart_upload(
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
        MultipartUpload: dict[str, object],
    ) -> dict[str, object]:
        del Bucket, MultipartUpload
        self.calls.append(f"complete:{Key}")
        parts = self.uploads.pop(UploadId)
        body = b"".join(parts[number] for number in sorted(parts))
        self.objects[Key] = (body, {"sha256": hashlib.sha256(body).hexdigest()})
        return {"ETag": '"complete"'}

    def abort_multipart_upload(self, *, Bucket: str, Key: str, UploadId: str) -> dict[str, object]:
        del Bucket
        self.calls.append(f"abort:{Key}:{UploadId}")
        self.uploads.pop(UploadId, None)
        return {}

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
        Metadata: dict[str, str],
        CacheControl: str,
    ) -> dict[str, object]:
        del Bucket, ContentType, CacheControl
        self.calls.append(f"put:{Key}")
        self.objects[Key] = (Body, Metadata.copy())
        return {"ETag": '"put"'}


class RuntimePublisherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.archive_body = (b"a" * (5 * MIB)) + (b"b" * (6 * MIB + 17))
        archive_sha = hashlib.sha256(self.archive_body).hexdigest()
        self.archive = self.root / f"sha256-{archive_sha}.tar.zst"
        self.archive.write_bytes(self.archive_body)
        entries = [
            {
                "path": "opt/conda/bin/python",
                "type": "file",
                "mode": 0o755,
                "size_bytes": 0,
                "sha256": "e" * 64,
            }
        ]
        tree_sha = hashlib.sha256(canonical_json(entries)).hexdigest()
        manifest = {
            "schema_version": 1,
            "runtime_version": "test-runtime",
            "runtime_digest": "sha256:" + tree_sha,
            "source": {
                "image": "ghcr.io/example/runtime",
                "image_digest": "sha256:" + "b" * 64,
                "build_sha": "c" * 40,
            },
            "compatibility": {
                "platform": "linux/amd64",
                "launcher_digest": "sha256:" + "d" * 64,
                "launcher_abi": "comfy-pod-launcher/v1",
            },
            "entrypoint": {"path": "opt/conda/bin/python", "argv": ["opt/conda/bin/python"]},
            "targets": ["/app/comfyui", "/opt/conda"],
            "selection_policy": {
                "targets": ["/app/comfyui", "/opt/conda"],
                "include_app": [],
                "excludes": [],
                "exclude_directory_names": [".git"],
            },
            "file_tree": {
                "entry_count": len(entries),
                "total_bytes": 0,
                "tree_sha256": tree_sha,
                "entries": entries,
            },
            "archive": {
                "format": "tar.zst",
                "object_name": self.archive.name,
                "size_bytes": len(self.archive_body),
                "sha256": archive_sha,
            },
        }
        self.manifest = self.root / "manifest.json"
        self.manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        self.manifest.write_bytes(self.manifest_bytes)
        self.item = publisher.prepare_publish(self.archive, self.manifest, prefix="runtime-test", channel="staging")
        self.config = publisher.StoreConfig(
            endpoint="https://objects.example.test",
            bucket="runtime-bucket",
            access_key_id="access",
            secret_access_key="secret",
            region="auto",
            prefix="runtime-test",
        )
        self.store = FakeObjectStore()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_missing_configuration_is_a_successful_skip(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            result = publisher.main(["--archive", str(self.archive), "--manifest", str(self.manifest)])
        self.assertEqual(result, 0)

    def test_partial_configuration_fails_closed(self) -> None:
        with patch.dict(os.environ, {"OBJECT_STORE_ENDPOINT": "https://objects.example.test"}, clear=True):
            result = publisher.main(["--archive", str(self.archive), "--manifest", str(self.manifest)])
        self.assertEqual(result, 2)

    def test_required_configuration_rejects_missing_credentials(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            result = publisher.main(
                ["--archive", str(self.archive), "--manifest", str(self.manifest), "--require-config"]
            )
        self.assertEqual(result, 2)

    def test_dry_run_without_configuration_still_validates_inputs(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            result = publisher.main(
                ["--archive", str(self.archive), "--manifest", str(self.manifest), "--dry-run"]
            )
        self.assertEqual(result, 0)

    def test_dry_run_does_not_construct_or_write_client(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OBJECT_STORE_ENDPOINT": "https://objects.example.test",
                "OBJECT_STORE_BUCKET": "runtime-bucket",
                "OBJECT_STORE_ACCESS_KEY_ID": "access",
                "OBJECT_STORE_SECRET_ACCESS_KEY": "secret",
                "OBJECT_STORE_PREFIX": "runtime-test",
            },
            clear=True,
        ), patch.object(publisher, "_build_client", side_effect=AssertionError("network client was built")):
            result = publisher.main(
                ["--archive", str(self.archive), "--manifest", str(self.manifest), "--dry-run"]
            )
        self.assertEqual(result, 0)

    def test_manifest_name_rejects_invalid_digest(self) -> None:
        with self.assertRaisesRegex(publisher.RuntimePublisherError, "invalid digest"):
            publisher._manifest_name("not-a-sha256")

    def test_multipart_success_and_channel_is_last_mutation(self) -> None:
        result = publisher.RuntimePublisher(self.store, self.config, part_size_bytes=5 * MIB).publish(self.item)
        self.assertEqual(result.status, "published")
        self.assertTrue(result.archive_uploaded)
        self.assertTrue(result.manifest_uploaded)
        self.assertTrue(result.channel_updated)
        mutations = [call for call in self.store.calls if call.startswith(("create:", "part:", "complete:", "put:", "abort:"))]
        self.assertTrue(mutations[-1].startswith("put:" + self.item.channel_key))
        self.assertIn(self.item.archive_key, self.store.objects)
        self.assertIn(self.item.manifest_key, self.store.objects)
        channel_body, _ = self.store.objects[self.item.channel_key]
        channel = json.loads(channel_body)
        self.assertEqual(channel["archive_key"], self.item.archive_key)
        self.assertEqual(channel["manifest_sha256"], self.item.manifest_sha256)
        self.assertEqual(channel["manifest_size_bytes"], len(self.manifest_bytes))
        self.assertEqual(channel["expanded_bytes"], 0)

    def test_existing_content_addressed_objects_are_reused(self) -> None:
        self.store.objects[self.item.archive_key] = (
            self.archive_body,
            {"sha256": self.item.archive_sha256},
        )
        self.store.objects[self.item.manifest_key] = (
            self.manifest_bytes,
            {"sha256": self.item.manifest_sha256},
        )
        channel_payload = publisher.build_channel_manifest(self.item)
        self.store.objects[self.item.channel_key] = (
            channel_payload,
            {"sha256": hashlib.sha256(channel_payload).hexdigest()},
        )
        result = publisher.RuntimePublisher(self.store, self.config, part_size_bytes=5 * MIB).publish(self.item)
        self.assertFalse(result.archive_uploaded)
        self.assertFalse(result.manifest_uploaded)
        self.assertFalse(result.channel_updated)
        self.assertFalse(any(call.startswith(("create:", "part:", "complete:", "put:")) for call in self.store.calls))

    def test_launcher_change_reuses_archive_but_publishes_distinct_manifest(self) -> None:
        self.store.objects[self.item.archive_key] = (
            self.archive_body,
            {"sha256": self.item.archive_sha256},
        )
        changed_manifest = json.loads(self.manifest_bytes)
        changed_manifest["compatibility"]["launcher_digest"] = "sha256:" + "f" * 64
        changed_bytes = json.dumps(changed_manifest, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        changed_path = self.root / "changed-manifest.json"
        changed_path.write_bytes(changed_bytes)
        changed_item = publisher.prepare_publish(
            self.archive,
            changed_path,
            prefix="runtime-test",
            channel="staging",
        )

        self.assertEqual(changed_item.archive_key, self.item.archive_key)
        self.assertNotEqual(changed_item.manifest_key, self.item.manifest_key)
        self.assertTrue(changed_item.manifest_key.endswith(f"sha256-{changed_item.manifest_sha256}.json"))

        result = publisher.RuntimePublisher(self.store, self.config, part_size_bytes=5 * MIB).publish(changed_item)

        self.assertFalse(result.archive_uploaded)
        self.assertTrue(result.manifest_uploaded)
        self.assertTrue(result.channel_updated)
        self.assertFalse(any(call.startswith("create:") for call in self.store.calls))
        self.assertIn(changed_item.manifest_key, self.store.objects)

    def test_existing_archive_with_wrong_metadata_is_rejected(self) -> None:
        self.store.objects[self.item.archive_key] = (self.archive_body, {"sha256": "0" * 64})
        with self.assertRaisesRegex(publisher.RuntimePublisherError, "content-addressed"):
            publisher.RuntimePublisher(self.store, self.config, part_size_bytes=5 * MIB).publish(self.item)
        self.assertFalse(any(call.startswith("create:") for call in self.store.calls))

    def test_multipart_failure_aborts_upload(self) -> None:
        self.store.fail_part = 2
        with self.assertRaisesRegex(publisher.RuntimePublisherError, "part upload"):
            publisher.RuntimePublisher(self.store, self.config, part_size_bytes=5 * MIB).publish(self.item)
        self.assertTrue(any(call.startswith("abort:" + self.item.archive_key) for call in self.store.calls))
        self.assertEqual(self.store.uploads, {})

    def test_source_change_is_detected_before_complete(self) -> None:
        original_upload_part = self.store.upload_part
        changed = False

        def mutate_after_first_part(**kwargs: object) -> dict[str, object]:
            nonlocal changed
            response = original_upload_part(**cast_upload_kwargs(kwargs))
            if not changed:
                changed = True
                self.archive.write_bytes(b"changed")
            return response

        self.store.upload_part = mutate_after_first_part  # type: ignore[method-assign]
        with self.assertRaisesRegex(publisher.RuntimePublisherError, "changed while uploading"):
            publisher.RuntimePublisher(self.store, self.config, part_size_bytes=5 * MIB).publish(self.item)
        self.assertTrue(any(call.startswith("abort:" + self.item.archive_key) for call in self.store.calls))


def cast_upload_kwargs(value: dict[str, object]) -> dict[str, object]:
    """Keep the injected fake method independent of boto3's permissive types."""

    return value


if __name__ == "__main__":
    unittest.main()
