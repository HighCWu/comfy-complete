"""Tests for the one-shot HTTPS runtime-materializer entrypoint."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib import request as urlrequest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from runtime_manifest import canonical_json  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "download_materialize_runtime", ROOT / "scripts" / "download_materialize_runtime.py"
)
assert SPEC and SPEC.loader
downloader = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = downloader
SPEC.loader.exec_module(downloader)


class _Response:
    def __init__(self, payload: bytes, url: str, *, content_length: str | None = None) -> None:
        self._stream = io.BytesIO(payload)
        self.status = 200
        self.headers = {"Content-Length": content_length} if content_length is not None else {}
        self._url = url

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        self._stream.close()

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def geturl(self) -> str:
        return self._url


class _Opener:
    def __init__(self, response: _Response) -> None:
        self.response = response

    def open(self, _request: urlrequest.Request, *, timeout: float) -> _Response:
        del timeout
        return self.response


class RuntimeMaterializerDownloaderTests(unittest.TestCase):
    def test_configuration_rejects_non_https_and_userinfo(self) -> None:
        for url in (
            "http://example.test/runtime",
            "https://user:password@example.test/runtime",
            "https://example.test/runtime#fragment",
        ):
            with self.subTest(url=url), self.assertRaises(downloader.RuntimeDownloadError) as context:
                downloader._validate_url(url)
            self.assertIn(context.exception.code, {"https_required", "configuration_invalid"})

    def test_redirect_handler_rejects_http_before_following(self) -> None:
        handler = downloader._HttpsRedirectHandler()
        request = urlrequest.Request("https://example.test/runtime")
        with self.assertRaisesRegex(downloader.RuntimeDownloadError, "https_required"):
            handler.redirect_request(request, None, 302, "found", {}, "http://example.test/runtime")

    def test_download_checks_content_length_digest_and_does_not_echo_url(self) -> None:
        payload = b"runtime-payload"
        url = "https://temporary.example/runtime.tar.zst?signature=short-lived"
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "archive.tar.zst"
            with patch.object(
                downloader,
                "_opener",
                return_value=_Opener(_Response(payload, url, content_length=str(len(payload)))),
            ):
                downloader._download_file(
                    url,
                    destination,
                    expected_sha256=hashlib.sha256(payload).hexdigest(),
                    expected_size_bytes=len(payload),
                    timeout_seconds=5,
                )
            self.assertEqual(destination.read_bytes(), payload)

            with patch.object(
                downloader,
                "_opener",
                return_value=_Opener(_Response(payload, url, content_length=str(len(payload) + 1))),
            ), self.assertRaisesRegex(downloader.RuntimeDownloadError, "download_size_mismatch"):
                downloader._download_file(
                    url,
                    destination,
                    expected_sha256=hashlib.sha256(payload).hexdigest(),
                    expected_size_bytes=len(payload),
                    timeout_seconds=5,
                )

    def _manifest(self, archive_sha256: str, archive_size_bytes: int) -> dict[str, object]:
        one_byte_sha256 = hashlib.sha256(b"x").hexdigest()
        entries = [
            {"path": "app/comfyui", "type": "directory", "mode": 0o755, "size_bytes": 0},
            {
                "path": "app/comfyui/main.py",
                "type": "file",
                "mode": 0o755,
                "size_bytes": 1,
                "sha256": one_byte_sha256,
            },
            {"path": "opt/conda", "type": "directory", "mode": 0o755, "size_bytes": 0},
            {"path": "opt/conda/bin", "type": "directory", "mode": 0o755, "size_bytes": 0},
            {
                "path": "opt/conda/bin/python",
                "type": "file",
                "mode": 0o755,
                "size_bytes": 1,
                "sha256": one_byte_sha256,
            },
        ]
        tree_sha256 = hashlib.sha256(canonical_json(entries)).hexdigest()
        return {
            "schema_version": 1,
            "runtime_version": "test-runtime",
            "runtime_digest": "sha256:" + tree_sha256,
            "source": {
                "image": "ghcr.io/example/runtime",
                "image_digest": "sha256:" + "a" * 64,
                "build_sha": "b" * 40,
            },
            "compatibility": {
                "platform": "linux/amd64",
                "launcher_digest": "sha256:" + "c" * 64,
                "launcher_abi": "comfy-pod-launcher/v1",
            },
            "entrypoint": {"path": "app/comfyui/main.py", "argv": ["app/comfyui/main.py"]},
            "targets": ["/app/comfyui", "/opt/conda"],
            "selection_policy": {
                "targets": ["/app/comfyui", "/opt/conda"],
                "include_app": [],
                "excludes": [],
                "exclude_directory_names": [".git"],
            },
            "file_tree": {
                "entry_count": len(entries),
                "total_bytes": 2,
                "tree_sha256": tree_sha256,
                "entries": entries,
            },
            "archive": {
                "format": "tar.zst",
                "object_name": "sha256-" + archive_sha256 + ".tar.zst",
                "size_bytes": archive_size_bytes,
                "sha256": archive_sha256,
            },
        }

    def test_run_binds_manifest_archive_identity_before_materializing(self) -> None:
        archive_payload = b"archive"
        archive_sha256 = hashlib.sha256(archive_payload).hexdigest()
        manifest = self._manifest(archive_sha256, len(archive_payload))
        manifest_payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        config = downloader.RuntimeDownloadConfig(
            archive_url="https://temporary.example/archive?signature=short-lived",
            manifest_url="https://temporary.example/manifest?signature=short-lived",
            archive_sha256=archive_sha256,
            archive_size_bytes=len(archive_payload),
            manifest_sha256=hashlib.sha256(manifest_payload).hexdigest(),
            manifest_size_bytes=len(manifest_payload),
            volume_root=Path("/runpod-volume"),
            timeout_seconds=5,
        )
        downloaded: list[tuple[str, Path]] = []
        materialized: list[tuple[Path, Path, Path]] = []

        def fake_download(url: str, destination: Path, **_kwargs: object) -> None:
            downloaded.append((url, destination))
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(manifest_payload if "manifest" in url else archive_payload)

        def fake_materialize(archive: Path, manifest_path: Path, volume: Path) -> dict[str, object]:
            materialized.append((archive, manifest_path, volume))
            return {
                "status": "materialized",
                "runtime_digest": manifest["runtime_digest"],
                "archive_size_bytes": len(archive_payload),
                "archive_sha256": archive_sha256,
                "entry_count": 5,
                "current_updated": True,
            }

        with tempfile.TemporaryDirectory() as volume:
            config = downloader.RuntimeDownloadConfig(
                **{**config.__dict__, "volume_root": Path(volume)}
            )
            with patch.object(downloader, "_download_file", side_effect=fake_download), patch.object(
                downloader, "materialize_runtime", side_effect=fake_materialize
            ), patch.object(downloader.shutil, "disk_usage", return_value=type("Usage", (), {"free": 1 << 40})()):
                result = downloader.run(config)

        self.assertEqual(result["status"], "materialized")
        self.assertEqual(result["current_updated"], True)
        self.assertEqual(len(downloaded), 2)
        self.assertEqual(materialized[0][0].name, "sha256-" + archive_sha256 + ".tar.zst")
        self.assertNotIn("signature", json.dumps(result))

    def test_main_error_is_bounded_and_does_not_echo_url(self) -> None:
        url = "https://temporary.example/archive?signature=do-not-print"
        with patch.object(
            downloader.RuntimeDownloadConfig,
            "from_environment",
            side_effect=downloader.RuntimeDownloadError("https_required"),
        ), patch("sys.stdout", new_callable=io.StringIO) as stdout:
            result = downloader.main([])
        self.assertEqual(result, 2)
        self.assertEqual(stdout.getvalue(), '{"error":"https_required","status":"error"}\n')
        self.assertNotIn(url, stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
