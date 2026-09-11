"""Tests for the one-shot HTTPS runtime-materializer entrypoint."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from urllib import request as urlrequest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from runtime_manifest import canonical_json  # noqa: E402
import materialize_runtime as materializer_module  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "download_materialize_runtime", ROOT / "scripts" / "download_materialize_runtime.py"
)
assert SPEC and SPEC.loader
downloader = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = downloader
SPEC.loader.exec_module(downloader)


class _Response:
    def __init__(
        self,
        payload: bytes,
        url: str,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        content_length: str | None = None,
    ) -> None:
        self._stream = io.BytesIO(payload)
        self.status = status
        self.headers = dict(headers or {})
        if content_length is not None:
            self.headers["Content-Length"] = content_length
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
        self.request: urlrequest.Request | None = None

    def open(self, request: urlrequest.Request, *, timeout: float) -> _Response:
        del timeout
        self.request = request
        return self.response


class _RangeOpener:
    def __init__(self, payload: bytes, url: str) -> None:
        self.payload = payload
        self.url = url
        self.requests: list[urlrequest.Request] = []

    def open(self, request: urlrequest.Request, *, timeout: float) -> _Response:
        del timeout
        self.requests.append(request)
        value = request.get_header("Range")
        assert value is not None
        start_text, end_text = value.removeprefix("bytes=").split("-", 1)
        start = int(start_text)
        end = int(end_text)
        body = self.payload[start : end + 1]
        return _Response(
            body,
            self.url,
            status=206,
            headers={
                "Content-Length": str(len(body)),
                "Content-Range": f"bytes {start}-{end}/{len(self.payload)}",
            },
        )


class RuntimeMaterializerDownloaderTests(unittest.TestCase):
    def test_configuration_accepts_content_addressed_pre_staged_pair(self) -> None:
        archive_sha256 = "a" * 64
        manifest_sha256 = "b" * 64
        root = "/runpod-volume"
        environment = {
            "RUNTIME_VOLUME_ROOT": root,
            "RUNTIME_ARCHIVE_PATH": f"{root}/.runtime-incoming/archives/sha256-{archive_sha256}.tar.zst",
            "RUNTIME_MANIFEST_PATH": f"{root}/.runtime-incoming/manifests/sha256-{manifest_sha256}.json",
            "RUNTIME_ARCHIVE_SHA256": archive_sha256,
            "RUNTIME_ARCHIVE_SIZE_BYTES": "7",
            "RUNTIME_MANIFEST_SHA256": manifest_sha256,
            "RUNTIME_MANIFEST_SIZE_BYTES": "9",
        }
        with patch.dict(os.environ, environment, clear=True):
            config = downloader.RuntimeDownloadConfig.from_environment()
        self.assertTrue(config.is_pre_staged)
        self.assertIsNone(config.archive_url)
        self.assertIsNone(config.manifest_url)
        self.assertEqual(config.archive_path, Path(environment["RUNTIME_ARCHIVE_PATH"]))
        self.assertEqual(config.manifest_path, Path(environment["RUNTIME_MANIFEST_PATH"]))

    def test_configuration_rejects_partial_or_traversing_pre_staged_pair(self) -> None:
        archive_sha256 = "a" * 64
        manifest_sha256 = "b" * 64
        base = {
            "RUNTIME_VOLUME_ROOT": "/runpod-volume",
            "RUNTIME_ARCHIVE_SHA256": archive_sha256,
            "RUNTIME_ARCHIVE_SIZE_BYTES": "7",
            "RUNTIME_MANIFEST_SHA256": manifest_sha256,
            "RUNTIME_MANIFEST_SIZE_BYTES": "9",
        }
        with patch.dict(
            os.environ,
            {
                **base,
                "RUNTIME_ARCHIVE_PATH": (
                    f"/runpod-volume/.runtime-incoming/archives/sha256-{archive_sha256}.tar.zst"
                ),
            },
            clear=True,
        ), self.assertRaisesRegex(downloader.RuntimeDownloadError, "configuration_invalid"):
            downloader.RuntimeDownloadConfig.from_environment()

        with patch.dict(
            os.environ,
            {
                **base,
                "RUNTIME_ARCHIVE_PATH": "/runpod-volume/.runtime-incoming/archives/../outside",
                "RUNTIME_MANIFEST_PATH": (
                    f"/runpod-volume/.runtime-incoming/manifests/sha256-{manifest_sha256}.json"
                ),
            },
            clear=True,
        ), self.assertRaisesRegex(downloader.RuntimeDownloadError, "configuration_invalid"):
            downloader.RuntimeDownloadConfig.from_environment()

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

    def test_range_download_is_contiguous_and_preserves_capability_url(self) -> None:
        payload = b"runtime-range-payload"
        url = "https://temporary.example/runtime.tar.zst?signature=short-lived"
        opener = _RangeOpener(payload, url)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "archive.tar.zst"
            with patch.object(downloader, "ARCHIVE_RANGE_BYTES", 4), patch.object(
                downloader, "_opener", return_value=opener
            ):
                downloader._download_range_chunks(
                    url,
                    destination,
                    expected_sha256=hashlib.sha256(payload).hexdigest(),
                    expected_size_bytes=len(payload),
                    timeout_seconds=5,
                )
            self.assertEqual(destination.read_bytes(), payload)
        self.assertEqual(
            [request.get_header("Range") for request in opener.requests],
            [
                "bytes=0-3",
                "bytes=4-7",
                "bytes=8-11",
                "bytes=12-15",
                "bytes=16-19",
                "bytes=20-20",
            ],
        )
        self.assertTrue(all(request.full_url == url for request in opener.requests))
        self.assertTrue(
            all(request.get_header("Accept-encoding") == "identity" for request in opener.requests)
        )

    def test_range_download_rejects_full_response_and_bad_ranges(self) -> None:
        payload = b"runtime-payload"
        url = "https://temporary.example/runtime.tar.zst?signature=short-lived"
        cases = (
            (
                _Response(
                    payload,
                    url,
                    status=200,
                    headers={"Content-Length": str(len(payload))},
                ),
                "download_range_required",
            ),
            (
                _Response(
                    payload[:4],
                    url,
                    status=206,
                    headers={
                        "Content-Length": "4",
                        "Content-Range": f"bytes 1-4/{len(payload)}",
                    },
                ),
                "download_range_mismatch",
            ),
            (
                _Response(
                    payload[:4],
                    url,
                    status=206,
                    headers={
                        "Content-Length": "4",
                        "Content-Range": f"bytes 0-7/{len(payload)}",
                    },
                ),
                "download_size_mismatch",
            ),
        )
        for response, error_code in cases:
            with self.subTest(error_code=error_code), tempfile.TemporaryDirectory() as directory:
                with patch.object(downloader, "ARCHIVE_RANGE_BYTES", 8), patch.object(
                    downloader, "_opener", return_value=_Opener(response)
                ), self.assertRaisesRegex(downloader.RuntimeDownloadError, error_code):
                    downloader._download_range_chunks(
                        url,
                        Path(directory) / "archive.tar.zst",
                        expected_sha256=hashlib.sha256(payload).hexdigest(),
                        expected_size_bytes=len(payload),
                        timeout_seconds=5,
                    )

    def test_range_download_rejects_short_and_overlong_bodies(self) -> None:
        payload = b"runtime-payload"
        url = "https://temporary.example/runtime.tar.zst?signature=short-lived"
        responses = (
            _Response(
                payload[:3],
                url,
                status=206,
                headers={
                    "Content-Length": "4",
                    "Content-Range": f"bytes 0-3/{len(payload)}",
                },
            ),
            _Response(
                payload[:5],
                url,
                status=206,
                headers={
                    "Content-Length": "4",
                    "Content-Range": f"bytes 0-3/{len(payload)}",
                },
            ),
        )
        for response in responses:
            with self.subTest(payload_length=len(response._stream.getvalue())), tempfile.TemporaryDirectory() as directory:
                with patch.object(downloader, "ARCHIVE_RANGE_BYTES", 4), patch.object(
                    downloader, "_opener", return_value=_Opener(response)
                ), self.assertRaisesRegex(downloader.RuntimeDownloadError, "download_size_mismatch"):
                    downloader._download_range_chunks(
                        url,
                        Path(directory) / "archive.tar.zst",
                        expected_sha256=hashlib.sha256(payload).hexdigest(),
                        expected_size_bytes=len(payload),
                        timeout_seconds=5,
                    )

    def test_result_post_is_https_json_without_authorization_header(self) -> None:
        payload = {"ok": True, "status": "materialized", "entry_count": 3}
        result_url = "https://temporary.example/result?signature=short-lived"
        opener = _Opener(_Response(b"", result_url, content_length="0"))
        with patch.object(downloader, "_opener", return_value=opener):
            downloader._post_result(result_url, payload, timeout_seconds=5)
        assert opener.request is not None
        self.assertEqual(opener.request.get_method(), "POST")
        self.assertEqual(json.loads(opener.request.data.decode("utf-8")), payload)
        self.assertNotIn("authorization", {key.lower() for key in opener.request.headers})

    def test_result_url_rejects_non_https(self) -> None:
        with self.assertRaisesRegex(downloader.RuntimeDownloadError, "https_required"):
            downloader._post_result(
                "http://temporary.example/result",
                {"ok": False, "error_code": "download_failed"},
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

    def _write_valid_staged_bundle(self, volume: Path) -> tuple[Path, Path, bytes, dict[str, object]]:
        if shutil.which("zstd") is None:
            self.skipTest("zstd CLI is required")
        tar_path = volume / "runtime.tar"
        archive_path = volume / "runtime.tar.zst"
        with tarfile.open(tar_path, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for path, mode, payload in (
                ("app/comfyui", 0o755, None),
                ("app/comfyui/main.py", 0o755, b"x"),
                ("opt/conda", 0o755, None),
                ("opt/conda/bin", 0o755, None),
                ("opt/conda/bin/python", 0o755, b"x"),
            ):
                info = tarfile.TarInfo(path)
                info.uid = 0
                info.gid = 0
                info.mtime = 0
                info.mode = mode
                info.pax_headers = {}
                if payload is None:
                    info.type = tarfile.DIRTYPE
                    info.size = 0
                    archive.addfile(info)
                else:
                    info.type = tarfile.REGTYPE
                    info.size = len(payload)
                    archive.addfile(info, io.BytesIO(payload))
        with archive_path.open("wb") as handle:
            completed = subprocess.run(
                ["zstd", "-q", "-T1", "--no-progress", "-c", str(tar_path)],
                stdout=handle,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
        archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        content_archive = volume / f"sha256-{archive_sha256}.tar.zst"
        archive_path.replace(content_archive)
        manifest = self._manifest(archive_sha256, content_archive.stat().st_size)
        manifest_payload = canonical_json(manifest) + b"\n"
        manifest_sha256 = hashlib.sha256(manifest_payload).hexdigest()
        staged_archive = volume / ".runtime-incoming" / "archives" / content_archive.name
        staged_manifest = volume / ".runtime-incoming" / "manifests" / f"sha256-{manifest_sha256}.json"
        staged_archive.parent.mkdir(parents=True)
        staged_manifest.parent.mkdir(parents=True)
        content_archive.replace(staged_archive)
        staged_manifest.write_bytes(manifest_payload)
        return staged_archive, staged_manifest, manifest_payload, manifest

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
        policies: list[object] = []
        probe_policy = object()

        def fake_download(url: str, destination: Path, **_kwargs: object) -> None:
            downloaded.append((url, destination))
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(manifest_payload if "manifest" in url else archive_payload)

        def fake_materialize(
            archive: Path,
            manifest_path: Path,
            volume: Path,
            **kwargs: object,
        ) -> dict[str, object]:
            materialized.append((archive, manifest_path, volume))
            policies.append(kwargs["mode_policy"])
            return {
                "status": "materialized",
                "runtime_digest": manifest["runtime_digest"],
                "archive_size_bytes": len(archive_payload),
                "archive_sha256": archive_sha256,
                "entry_count": 5,
                "materialized_bytes": manifest["file_tree"]["total_bytes"],
                "current_updated": True,
            }

        with tempfile.TemporaryDirectory() as volume:
            Path(volume).chmod(0o755)
            config = downloader.RuntimeDownloadConfig(
                **{**config.__dict__, "volume_root": Path(volume)}
            )
            with patch.object(downloader, "_download_file", side_effect=fake_download), patch.object(
                downloader, "_download_range_chunks", side_effect=fake_download
            ), patch.object(
                downloader,
                "probe_volume_mode_capability",
                return_value=probe_policy,
            ), patch.object(
                downloader, "materialize_runtime", side_effect=fake_materialize
            ), patch.object(downloader.shutil, "disk_usage", return_value=type("Usage", (), {"free": 1 << 40})()):
                result = downloader.run(config)

        self.assertEqual(result["status"], "materialized")
        self.assertEqual(result["current_updated"], True)
        self.assertEqual(result["downloaded_bytes"], len(archive_payload))
        self.assertEqual(len(downloaded), 2)
        self.assertEqual(materialized[0][0].name, "sha256-" + archive_sha256 + ".tar.zst")
        self.assertEqual(policies, [probe_policy])
        self.assertNotIn("signature", json.dumps(result))

    def test_pre_staged_run_verifies_in_place_without_download_or_cleanup(self) -> None:
        archive_payload = b"archive"
        archive_sha256 = hashlib.sha256(archive_payload).hexdigest()
        manifest = self._manifest(archive_sha256, len(archive_payload))
        manifest_payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        manifest_sha256 = hashlib.sha256(manifest_payload).hexdigest()

        with tempfile.TemporaryDirectory() as directory:
            volume = Path(directory)
            archive_path = volume / ".runtime-incoming" / "archives" / f"sha256-{archive_sha256}.tar.zst"
            manifest_path = volume / ".runtime-incoming" / "manifests" / f"sha256-{manifest_sha256}.json"
            archive_path.parent.mkdir(parents=True)
            manifest_path.parent.mkdir(parents=True)
            archive_path.write_bytes(archive_payload)
            manifest_path.write_bytes(manifest_payload)
            config = downloader.RuntimeDownloadConfig(
                archive_url=None,
                manifest_url=None,
                archive_sha256=archive_sha256,
                archive_size_bytes=len(archive_payload),
                manifest_sha256=manifest_sha256,
                manifest_size_bytes=len(manifest_payload),
                volume_root=volume,
                timeout_seconds=5,
                archive_path=archive_path,
                manifest_path=manifest_path,
            )
            materialized: list[tuple[Path, Path, Path]] = []

            def fake_materialize(
                archive: Path,
                staged_manifest: Path,
                mounted_volume: Path,
                **_kwargs: object,
            ) -> dict[str, object]:
                materialized.append((archive, staged_manifest, mounted_volume))
                return {
                    "status": "materialized",
                    "runtime_digest": manifest["runtime_digest"],
                    "archive_sha256": archive_sha256,
                    "archive_size_bytes": len(archive_payload),
                    "entry_count": 5,
                    "materialized_bytes": manifest["file_tree"]["total_bytes"],
                    "current_updated": True,
                }

            with patch.object(downloader, "_download_file") as download_manifest, patch.object(
                downloader, "_download_range_chunks"
            ) as download_archive, patch.object(
                downloader,
                "probe_volume_mode_capability",
                return_value=object(),
            ), patch.object(downloader, "materialize_runtime", side_effect=fake_materialize):
                result = downloader.run(config)

            self.assertEqual(result["verified_archive_bytes"], len(archive_payload))
            self.assertNotIn("downloaded_bytes", result)
            self.assertEqual(materialized, [(archive_path, manifest_path, volume)])
            self.assertEqual(archive_path.read_bytes(), archive_payload)
            self.assertEqual(manifest_path.read_bytes(), manifest_payload)
            download_manifest.assert_not_called()
            download_archive.assert_not_called()

    def test_pre_staged_run_rejects_symlink_input_before_materializing(self) -> None:
        archive_payload = b"archive"
        archive_sha256 = hashlib.sha256(archive_payload).hexdigest()
        manifest = self._manifest(archive_sha256, len(archive_payload))
        manifest_payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        manifest_sha256 = hashlib.sha256(manifest_payload).hexdigest()
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            volume = Path(directory)
            archive_path = volume / ".runtime-incoming" / "archives" / f"sha256-{archive_sha256}.tar.zst"
            manifest_path = volume / ".runtime-incoming" / "manifests" / f"sha256-{manifest_sha256}.json"
            archive_path.parent.mkdir(parents=True)
            manifest_path.parent.mkdir(parents=True)
            archive_path.symlink_to(Path(outside) / "archive.tar.zst")
            manifest_path.write_bytes(manifest_payload)
            config = downloader.RuntimeDownloadConfig(
                archive_url=None,
                manifest_url=None,
                archive_sha256=archive_sha256,
                archive_size_bytes=len(archive_payload),
                manifest_sha256=manifest_sha256,
                manifest_size_bytes=len(manifest_payload),
                volume_root=volume,
                timeout_seconds=5,
                archive_path=archive_path,
                manifest_path=manifest_path,
            )
            with patch.object(downloader, "materialize_runtime") as materialize:
                with self.assertRaisesRegex(downloader.RuntimeDownloadError, "staged_path_invalid"):
                    downloader.run(config)
            materialize.assert_not_called()

    def test_pre_staged_logs_are_bounded_and_path_free(self) -> None:
        archive_payload = b"archive"
        archive_sha256 = hashlib.sha256(archive_payload).hexdigest()
        manifest = self._manifest(archive_sha256, len(archive_payload))
        manifest_payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        manifest_sha256 = hashlib.sha256(manifest_payload).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            volume = Path(directory)
            archive_path = volume / ".runtime-incoming" / "archives" / f"sha256-{archive_sha256}.tar.zst"
            manifest_path = volume / ".runtime-incoming" / "manifests" / f"sha256-{manifest_sha256}.json"
            archive_path.parent.mkdir(parents=True)
            manifest_path.parent.mkdir(parents=True)
            archive_path.write_bytes(archive_payload)
            manifest_path.write_bytes(manifest_payload)
            config = downloader.RuntimeDownloadConfig(
                archive_url=None,
                manifest_url=None,
                archive_sha256=archive_sha256,
                archive_size_bytes=len(archive_payload),
                manifest_sha256=manifest_sha256,
                manifest_size_bytes=len(manifest_payload),
                volume_root=volume,
                timeout_seconds=5,
                archive_path=archive_path,
                manifest_path=manifest_path,
            )
            def fake_materialize(*_args: object, **kwargs: object) -> dict[str, object]:
                callback = kwargs["progress_callback"]
                assert callable(callback)
                callback("archive_verify", "start", 0, len(archive_payload))
                callback("archive_verify", "progress", len(archive_payload), len(archive_payload))
                callback("archive_verify", "end", len(archive_payload), len(archive_payload))
                callback("extraction", "start", 0, None)
                callback("extraction", "end", 1, 1)
                callback("tree_verify", "start", 0, None)
                callback("tree_verify", "end", 1, 1)
                return {
                    "status": "materialized",
                    "runtime_digest": manifest["runtime_digest"],
                    "archive_sha256": archive_sha256,
                    "archive_size_bytes": len(archive_payload),
                    "entry_count": 5,
                    "materialized_bytes": 1,
                    "current_updated": True,
                }

            with patch.object(downloader, "probe_volume_mode_capability", return_value=object()), patch.object(
                downloader,
                "materialize_runtime",
                side_effect=fake_materialize,
            ), patch("sys.stderr", new_callable=io.StringIO) as stderr:
                downloader.run(config)
            logs = stderr.getvalue()
            self.assertIn('"phase":"archive_verify_start"', logs)
            self.assertIn('"phase":"archive_verify_end"', logs)
            self.assertIn('"phase":"materialization_start"', logs)
            self.assertIn('"phase":"materialization_end"', logs)
            self.assertIn('"phase":"archive_verify"', logs)
            self.assertNotIn(str(volume), logs)
            self.assertNotIn("https://", logs)

    def test_pre_staged_archive_is_hashed_once_by_authoritative_materializer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            volume = Path(directory)
            archive_path, manifest_path, manifest_payload, manifest = self._write_valid_staged_bundle(volume)
            archive_sha256 = manifest["archive"]["sha256"]
            assert isinstance(archive_sha256, str)
            config = downloader.RuntimeDownloadConfig(
                archive_url=None,
                manifest_url=None,
                archive_sha256=archive_sha256,
                archive_size_bytes=manifest["archive"]["size_bytes"],
                manifest_sha256=hashlib.sha256(manifest_payload).hexdigest(),
                manifest_size_bytes=len(manifest_payload),
                volume_root=volume,
                timeout_seconds=5,
                archive_path=archive_path,
                manifest_path=manifest_path,
            )
            Path(volume).chmod(0o755)
            original_hash = materializer_module._hash_regular_file
            with patch.object(
                materializer_module,
                "_hash_regular_file",
                wraps=original_hash,
            ) as hash_file:
                result = downloader.run(config)

            archive_calls = [
                call
                for call in hash_file.call_args_list
                if call.args and Path(call.args[0]) == archive_path
            ]
            self.assertEqual(len(archive_calls), 1)
            self.assertEqual(result["verified_archive_bytes"], config.archive_size_bytes)
            self.assertEqual(result["archive_sha256"], archive_sha256)
            self.assertEqual(result["manifest_sha256"], config.manifest_sha256)

    def test_pre_staged_manifest_replacement_after_wrapper_verification_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            volume = Path(directory)
            archive_path, manifest_path, manifest_payload, manifest = self._write_valid_staged_bundle(volume)
            archive_sha256 = manifest["archive"]["sha256"]
            assert isinstance(archive_sha256, str)
            manifest_value = json.loads(manifest_payload.decode("utf-8"))
            manifest_value["runtime_version"] = "replacement-after-verification"
            replacement_payload = canonical_json(manifest_value) + b"\n"
            config = downloader.RuntimeDownloadConfig(
                archive_url=None,
                manifest_url=None,
                archive_sha256=archive_sha256,
                archive_size_bytes=manifest["archive"]["size_bytes"],
                manifest_sha256=hashlib.sha256(manifest_payload).hexdigest(),
                manifest_size_bytes=len(manifest_payload),
                volume_root=volume,
                timeout_seconds=5,
                archive_path=archive_path,
                manifest_path=manifest_path,
            )
            Path(volume).chmod(0o755)
            original_probe = downloader.probe_volume_mode_capability

            def replace_after_probe(root: Path, verified: object) -> object:
                policy = original_probe(root, verified)
                manifest_path.write_bytes(replacement_payload)
                return policy

            with patch.object(
                downloader,
                "probe_volume_mode_capability",
                side_effect=replace_after_probe,
            ):
                result = downloader.run(config)

            runtime_hex = str(manifest["runtime_digest"])[len("sha256:") :]
            published_manifest = (volume / "runtimes" / runtime_hex / "manifest.json").read_bytes()
            self.assertEqual(published_manifest, manifest_payload)
            self.assertEqual(result["runtime_digest"], manifest["runtime_digest"])
            self.assertNotEqual(published_manifest, replacement_payload)

    def test_run_forwards_verified_materialized_bytes_from_materializer(self) -> None:
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

        def fake_download(_url: str, destination: Path, **_kwargs: object) -> None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(manifest_payload if destination.name == "manifest.json" else archive_payload)

        materializer_result = {
            "status": "materialized",
            "runtime_digest": manifest["runtime_digest"],
            "archive_size_bytes": len(archive_payload),
            "archive_sha256": archive_sha256,
            "entry_count": 5,
            "current_updated": True,
            "materialized_bytes": 123,
            "volume_total_bytes": 1000,
            "volume_free_bytes_before": 900,
            "volume_free_bytes_after": 700,
            "volume_total_inodes": 100,
            "volume_free_inodes_before": 90,
            "volume_free_inodes_after": 80,
        }
        with tempfile.TemporaryDirectory() as volume:
            Path(volume).chmod(0o755)
            config = downloader.RuntimeDownloadConfig(
                **{**config.__dict__, "volume_root": Path(volume)}
            )
            with patch.object(downloader, "_download_file", side_effect=fake_download), patch.object(
                downloader, "_download_range_chunks", side_effect=fake_download
            ), patch.object(
                downloader, "materialize_runtime", return_value=materializer_result
            ), patch.object(downloader.shutil, "disk_usage", return_value=type("Usage", (), {"free": 1 << 40})()):
                result = downloader.run(config)
        self.assertEqual(result["downloaded_bytes"], len(archive_payload))
        self.assertEqual(result["materialized_bytes"], 123)
        self.assertEqual(result["volume_free_bytes_before"], 900)
        self.assertEqual(result["volume_free_bytes_after"], 700)
        self.assertEqual(result["volume_free_inodes_before"], 90)
        self.assertEqual(result["volume_free_inodes_after"], 80)

    def test_run_rejects_mode_capability_before_archive_download(self) -> None:
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

        def fake_manifest_download(_url: str, destination: Path, **_kwargs: object) -> None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(manifest_payload)

        with tempfile.TemporaryDirectory() as volume:
            Path(volume).chmod(0o755)
            config = downloader.RuntimeDownloadConfig(
                **{**config.__dict__, "volume_root": Path(volume)}
            )
            with patch.object(downloader, "_download_file", side_effect=fake_manifest_download), patch.object(
                downloader, "_download_range_chunks"
            ) as archive_download, patch.object(
                downloader,
                "probe_volume_mode_capability",
                side_effect=downloader.RuntimeMaterializerError(
                    "materialized_mode_mismatch",
                    diagnostics={"entry_kind": 2, "expected_mode": 0o755, "actual_mode": 0o700},
                ),
            ), patch.object(
                downloader.shutil,
                "disk_usage",
                return_value=type("Usage", (), {"free": 1 << 40})(),
            ), self.assertRaisesRegex(downloader.RuntimeDownloadError, "materialized_mode_mismatch"):
                downloader.run(config)

        archive_download.assert_not_called()

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

    def _config_with_result_url(self) -> downloader.RuntimeDownloadConfig:
        return downloader.RuntimeDownloadConfig(
            archive_url="https://temporary.example/archive?signature=short-lived",
            manifest_url="https://temporary.example/manifest?signature=short-lived",
            archive_sha256="a" * 64,
            archive_size_bytes=1,
            manifest_sha256="b" * 64,
            manifest_size_bytes=1,
            volume_root=Path("/runpod-volume"),
            timeout_seconds=5,
            result_url="https://temporary.example/result?signature=short-lived",
        )

    def test_failed_materialization_posts_failure_and_stays_failed(self) -> None:
        config = self._config_with_result_url()
        with patch.object(
            downloader.RuntimeDownloadConfig, "from_environment", return_value=config
        ), patch.object(
            downloader, "run", side_effect=downloader.RuntimeDownloadError("download_failed")
        ), patch.object(downloader, "_post_result") as report, patch(
            "sys.stdout", new_callable=io.StringIO
        ) as stdout:
            result = downloader.main([])
        self.assertEqual(result, 2)
        report.assert_called_once_with(
            config.result_url,
            {"ok": False, "error_code": "download_failed"},
            timeout_seconds=config.timeout_seconds,
        )
        self.assertEqual(stdout.getvalue(), '{"error":"download_failed","status":"error"}\n')

    def test_failure_callback_preserves_volume_capacity_error_code(self) -> None:
        config = self._config_with_result_url()
        with patch.object(
            downloader.RuntimeDownloadConfig, "from_environment", return_value=config
        ), patch.object(
            downloader, "run", side_effect=downloader.RuntimeDownloadError("volume_write_failed")
        ), patch.object(downloader, "_post_result") as report, patch(
            "sys.stdout", new_callable=io.StringIO
        ) as stdout:
            result = downloader.main([])
        self.assertEqual(result, 2)
        report.assert_called_once_with(
            config.result_url,
            {"ok": False, "error_code": "volume_write_failed"},
            timeout_seconds=config.timeout_seconds,
        )
        self.assertEqual(stdout.getvalue(), '{"error":"volume_write_failed","status":"error"}\n')

    def test_failure_callback_forwards_bounded_capacity_diagnostics(self) -> None:
        config = self._config_with_result_url()
        diagnostics = {
            "volume_total_bytes": 1000,
            "volume_free_bytes": 20,
            "volume_total_inodes": 100,
            "volume_free_inodes": 0,
        }
        with patch.object(
            downloader.RuntimeDownloadConfig, "from_environment", return_value=config
        ), patch.object(
            downloader,
            "run",
            side_effect=downloader.RuntimeDownloadError(
                "volume_capacity_exhausted", diagnostics=diagnostics
            ),
        ), patch.object(downloader, "_post_result") as report, patch(
            "sys.stdout", new_callable=io.StringIO
        ) as stdout:
            result = downloader.main([])
        self.assertEqual(result, 2)
        report.assert_called_once_with(
            config.result_url,
            {"ok": False, "error_code": "volume_capacity_exhausted", "diagnostics": diagnostics},
            timeout_seconds=config.timeout_seconds,
        )
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {"status": "error", "error": "volume_capacity_exhausted", "diagnostics": diagnostics},
        )

    def test_success_result_report_failure_is_fail_closed(self) -> None:
        config = self._config_with_result_url()
        materialized = {
            "status": "materialized",
            "runtime_digest": "sha256:" + "c" * 64,
            "archive_sha256": config.archive_sha256,
            "archive_size_bytes": config.archive_size_bytes,
            "downloaded_bytes": config.archive_size_bytes,
            "manifest_sha256": config.manifest_sha256,
            "manifest_size_bytes": config.manifest_size_bytes,
            "entry_count": 3,
            "current_updated": True,
        }
        with patch.object(
            downloader.RuntimeDownloadConfig, "from_environment", return_value=config
        ), patch.object(downloader, "run", return_value=materialized), patch.object(
            downloader,
            "_post_result",
            side_effect=downloader.RuntimeDownloadError("result_report_failed"),
        ) as report, patch("sys.stdout", new_callable=io.StringIO) as stdout:
            result = downloader.main([])
        self.assertEqual(result, 2)
        report.assert_called_once_with(
            config.result_url,
            {"ok": True, **materialized},
            timeout_seconds=config.timeout_seconds,
        )
        self.assertEqual(stdout.getvalue(), '{"error":"result_report_failed","status":"error"}\n')
        self.assertNotIn("materialized", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
