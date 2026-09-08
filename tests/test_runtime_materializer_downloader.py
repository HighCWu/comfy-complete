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
                downloader, "materialize_runtime", side_effect=fake_materialize
            ), patch.object(downloader.shutil, "disk_usage", return_value=type("Usage", (), {"free": 1 << 40})()):
                result = downloader.run(config)

        self.assertEqual(result["status"], "materialized")
        self.assertEqual(result["current_updated"], True)
        self.assertEqual(result["downloaded_bytes"], len(archive_payload))
        self.assertEqual(len(downloaded), 2)
        self.assertEqual(materialized[0][0].name, "sha256-" + archive_sha256 + ".tar.zst")
        self.assertNotIn("signature", json.dumps(result))

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
