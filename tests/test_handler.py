"""
Unit tests for the ComfyUI RunPod handler (docker/runpod/handler.py).

Covers the pure functions and the platform-specific code paths that were
added when the worker was forked from upstream runpod-worker-comfyui:

  - validate_input                (input shape + userId/promptId extraction)
  - object_store_config/state     (optional S3-compatible integration gating)
  - upload_to_r2                  (backward-compatible object-store upload)
  - persist_terminal_result       (compact R2 result redundancy)
  - _get_comfyui_pid              (PID file parsing)
  - _is_comfyui_process_alive     (os.kill signal-0 probe)
  - _comfy_server_status          (HTTP reachability probe)
  - check_server                  (retry loop + PID-aware termination)
  - _attempt_websocket_reconnect  (reconnect w/ HTTP-status precondition)
  - upload_images                 (base64 -> ComfyUI /upload/image)

The full `handler()` generator is deliberately NOT tested here -- it
requires mocking runpod.serverless, the websocket frame protocol,
requests.post/get, and the multi-yield control flow. The risk/coverage
trade-off is poor for in-process tests; the integration coverage lives
in the API-side queue consumer tests + the e2e/workflow.spec.ts
Playwright spec that drives the real handler through the mock.

## Running

  cd packages/comfy-complete
  python -m pytest tests/test_handler.py -v        # CI mode
  python -m unittest tests.test_handler -v          # zero-dep mode

Both runners work -- pytest discovers unittest.TestCase subclasses, and
the test file uses only stdlib + the handler's own deps (runpod,
websocket-client, requests, boto3) which are already required by the
Docker image. No new test-only dependencies.
"""

import sys
import os
import json
import unittest
from unittest.mock import patch, MagicMock, mock_open, Mock

# The handler lives at docker/runpod/handler.py (vendored into the
# comfy-complete Docker image at /handler.py). Mirror that layout so
# `import handler` and the handler's `from network_volume import ...`
# both resolve from the same directory.
ROOT = os.path.abspath(os.path.dirname(__file__) + "/..")
sys.path.insert(0, os.path.join(ROOT, "docker", "runpod"))

import handler  # noqa: E402


# ---------------------------------------------------------------------------
# validate_input
# ---------------------------------------------------------------------------


class TestStreamSafeErrorChunks(unittest.TestCase):
    """RunPod SDK must not swallow terminal handler details."""

    def test_error_chunk_avoids_reserved_top_level_error_key(self):
        chunk = handler.error_chunk("ComfyUI failed", ["detail"])
        self.assertEqual(chunk["type"], "error")
        self.assertEqual(chunk["message"], "ComfyUI failed")
        self.assertEqual(chunk["details"], ["detail"])
        self.assertNotIn("error", chunk)

    def test_handler_validation_error_is_stream_safe(self):
        chunks = list(handler.handler({"id": "job-1", "input": None}))
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["type"], "error")
        self.assertIn("Please provide input", chunks[0]["message"])
        self.assertNotIn("error", chunks[0])

    @patch("handler.check_server", return_value=False)
    def test_handler_startup_error_is_stream_safe(self, _check_server):
        chunks = list(
            handler.handler(
                {"id": "job-2", "input": {"workflow": {"1": {}}}}
            )
        )
        self.assertEqual(len(chunks), 1)
        self.assertIn("not reachable", chunks[0]["message"])
        self.assertNotIn("error", chunks[0])

    @patch("handler.check_server")
    def test_handler_rejects_partial_object_store_before_startup(self, check_server):
        with patch.dict(
            os.environ,
            {
                "OBJECT_STORE_ENDPOINT": "https://objects.example.test",
                "OBJECT_STORE_BUCKET": "objects-bucket",
            },
            clear=True,
        ):
            chunks = list(
                handler.handler(
                    {"id": "job-3", "input": {"workflow": {"1": {}}}}
                )
            )
        self.assertEqual(len(chunks), 1)
        self.assertIn("Object storage configuration error", chunks[0]["message"])
        self.assertIn("partial", chunks[0]["message"])
        check_server.assert_not_called()


class TestValidateInput(unittest.TestCase):
    """validate_input is the first line of defense against malformed jobs."""

    def test_workflow_only(self):
        data, err = handler.validate_input({"workflow": {"key": "value"}})
        self.assertIsNone(err)
        self.assertEqual(data["workflow"], {"key": "value"})
        # comfy fields default to None when not provided
        self.assertIsNone(data["images"])
        self.assertIsNone(data["userId"])
        self.assertIsNone(data["promptId"])
        self.assertIsNone(data["comfy_org_api_key"])

    def test_workflow_and_images(self):
        data, err = handler.validate_input({
            "workflow": {"key": "value"},
            "images": [{"name": "img.png", "image": "BASE64"}],
        })
        self.assertIsNone(err)
        self.assertEqual(len(data["images"]), 1)

    def test_comfy_userId_promptId_extracted(self):
        # The R2 key construction in handler() depends on these two fields
        # being surfaced. A regression that dropped them would silently
        # skip R2 upload and fall back to base64 -- tripping the 10MB
        # output cap for any non-trivial batch.
        data, err = handler.validate_input({
            "workflow": {"key": "value"},
            "userId": "user-abc",
            "promptId": "prompt-xyz",
        })
        self.assertIsNone(err)
        self.assertEqual(data["userId"], "user-abc")
        self.assertEqual(data["promptId"], "prompt-xyz")

    def test_comfy_org_api_key_passthrough(self):
        # Per-request API key overrides env var -- tested in queue_workflow
        # but here we just confirm validate_input surfaces it.
        data, err = handler.validate_input({
            "workflow": {},
            "comfy_org_api_key": "sk-test-123",
        })
        self.assertIsNone(err)
        self.assertEqual(data["comfy_org_api_key"], "sk-test-123")

    def test_missing_workflow_rejected(self):
        _, err = handler.validate_input({"images": []})
        self.assertEqual(err, "Missing 'workflow' parameter")

    def test_images_must_have_name_and_image(self):
        # ComfyUI /upload/image expects both -- a missing 'image' key would
        # produce a KeyError deep in upload_images instead of a clean reject.
        _, err = handler.validate_input({
            "workflow": {},
            "images": [{"name": "img.png"}],  # missing 'image'
        })
        self.assertEqual(err, "'images' must be a list of objects with 'name' and 'image' keys")

    def test_invalid_json_string(self):
        _, err = handler.validate_input("not json")
        self.assertEqual(err, "Invalid JSON format in input")

    def test_valid_json_string(self):
        # RunPod sometimes delivers the input as a JSON string instead of
        # an already-parsed object (depends on client SDK). validate_input
        # tolerates both.
        data, err = handler.validate_input('{"workflow": {"k": 1}}')
        self.assertIsNone(err)
        self.assertEqual(data["workflow"], {"k": 1})

    def test_none_input(self):
        _, err = handler.validate_input(None)
        self.assertEqual(err, "Please provide input")


# ---------------------------------------------------------------------------
# Optional object-store configuration and upload
# ---------------------------------------------------------------------------


class TestObjectStoreConfiguration(unittest.TestCase):
    def test_unconfigured_is_explicitly_disabled(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(handler.object_store_state(), handler.OBJECT_STORE_DISABLED)
            self.assertIsNone(handler.object_store_config())
            self.assertFalse(handler.r2_configured())

    def test_partial_configuration_is_explicitly_invalid(self):
        with patch.dict(
            os.environ,
            {
                "R2_ENDPOINT": "https://example.r2.cloudflarestorage.com",
                "R2_BUCKET": "example-bucket",
                "R2_ACCESS_KEY_ID": "access",
            },
            clear=True,
        ):
            self.assertEqual(handler.object_store_state(), handler.OBJECT_STORE_INVALID)
            with self.assertRaisesRegex(
                handler.ObjectStoreConfigurationError,
                "partial.*R2_.*secret_access_key",
            ):
                handler.object_store_config()

    def test_generic_names_are_preferred_and_not_mixed_with_legacy_names(self):
        with patch.dict(
            os.environ,
            {
                "OBJECT_STORE_ENDPOINT": "https://objects.example.test",
                "OBJECT_STORE_BUCKET": "generic-bucket",
                "OBJECT_STORE_ACCESS_KEY_ID": "generic-access",
                "OBJECT_STORE_SECRET_ACCESS_KEY": "generic-secret",
                # A complete legacy group must not override the preferred one.
                "R2_ENDPOINT": "https://legacy.example.test",
                "R2_BUCKET": "legacy-bucket",
                "R2_ACCESS_KEY_ID": "legacy-access",
                "R2_SECRET_ACCESS_KEY": "legacy-secret",
            },
            clear=True,
        ):
            config = handler.object_store_config()
            self.assertIsNotNone(config)
            self.assertEqual(config.endpoint, "https://objects.example.test")
            self.assertEqual(config.bucket, "generic-bucket")

    def test_partial_preferred_group_does_not_fall_back_to_complete_legacy_group(self):
        with patch.dict(
            os.environ,
            {
                "OBJECT_STORE_ENDPOINT": "https://objects.example.test",
                "R2_ENDPOINT": "https://legacy.example.test",
                "R2_BUCKET": "legacy-bucket",
                "R2_ACCESS_KEY_ID": "legacy-access",
                "R2_SECRET_ACCESS_KEY": "legacy-secret",
            },
            clear=True,
        ):
            self.assertEqual(handler.object_store_state(), handler.OBJECT_STORE_INVALID)
            with self.assertRaisesRegex(handler.ObjectStoreConfigurationError, "OBJECT_STORE"):
                handler.object_store_config()

    def test_non_local_http_endpoint_is_rejected(self):
        with patch.dict(
            os.environ,
            {
                "OBJECT_STORE_ENDPOINT": "http://objects.example.test",
                "OBJECT_STORE_BUCKET": "objects-bucket",
                "OBJECT_STORE_ACCESS_KEY_ID": "access",
                "OBJECT_STORE_SECRET_ACCESS_KEY": "secret",
            },
            clear=True,
        ):
            self.assertEqual(handler.object_store_state(), handler.OBJECT_STORE_INVALID)
            with self.assertRaisesRegex(handler.ObjectStoreConfigurationError, "HTTPS"):
                handler.object_store_config()

    def test_local_http_endpoint_is_allowed_for_development_facades(self):
        with patch.dict(
            os.environ,
            {
                "OBJECT_STORE_ENDPOINT": "http://localhost:3080/r2",
                "OBJECT_STORE_BUCKET": "objects-bucket",
                "OBJECT_STORE_ACCESS_KEY_ID": "access",
                "OBJECT_STORE_SECRET_ACCESS_KEY": "secret",
            },
            clear=True,
        ):
            self.assertEqual(handler.object_store_state(), handler.OBJECT_STORE_CONFIGURED)


class TestUploadToR2(unittest.TestCase):
    """
    upload_to_r2 is the compatibility entry point for the S3-compatible
    bypass for RunPod's 10MB output cap.

    Env-var gating is the critical contract: if all object-store variables are
    unset, the function returns None WITHOUT raising -- the caller falls back
    to base64. A partial configuration is different: the function must raise
    so a typo cannot silently select the inline path.
    """

    BYTES = b"\x89PNG\r\n\x1a\nfake-png-bytes"
    KEY = "output/user-abc/prompt-xyz/ComfyUI_00001_.png"
    CT = "image/png"

    def test_returns_none_when_env_missing(self):
        # Clear all R2 env vars -- even if some are set in the test
        # environment, the function must treat any missing one as "off".
        with patch.dict(os.environ, {}, clear=False):
            for k in ("R2_ENDPOINT", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
                os.environ.pop(k, None)
            result = handler.upload_to_r2(self.BYTES, self.KEY, self.CT)
            self.assertIsNone(result)

    def test_r2_config_partial_state_is_not_disabled(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(handler.r2_configured())
        with patch.dict(
            os.environ,
            {
                "R2_ENDPOINT": "https://example.r2.cloudflarestorage.com",
                "R2_BUCKET": "example-bucket",
                "R2_ACCESS_KEY_ID": "access",
            },
            clear=True,
        ):
            self.assertEqual(handler.object_store_state(), handler.OBJECT_STORE_INVALID)
            with self.assertRaises(handler.ObjectStoreConfigurationError):
                handler.r2_configured()
            with self.assertRaises(handler.ObjectStoreConfigurationError):
                handler.upload_to_r2(self.BYTES, self.KEY, self.CT)

    def test_returns_key_on_success(self):
        # boto3 is imported lazily inside upload_to_r2 (`import boto3`
        # is inside the function body), so we can't patch `handler.boto3`.
        # Inject a mock module into sys.modules -- the function's
        # `import boto3` resolves to it.
        mock_boto3 = MagicMock()
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        with patch.dict(sys.modules, {"boto3": mock_boto3}), \
             patch.dict(os.environ, {
                 "OBJECT_STORE_ENDPOINT": "https://objects.example.test",
                 "OBJECT_STORE_BUCKET": "example-bucket",
                 "OBJECT_STORE_ACCESS_KEY_ID": "AKIA-test",
                 "OBJECT_STORE_SECRET_ACCESS_KEY": "secret-test",
             }, clear=True):
            result = handler.upload_to_r2(self.BYTES, self.KEY, self.CT)

        self.assertEqual(result, self.KEY)
        # Verify put_object received the right args -- particularly the
        # path-style config which is load-bearing for the dev Worker route.
        mock_client.put_object.assert_called_once()
        call_kwargs = mock_client.put_object.call_args.kwargs
        self.assertEqual(call_kwargs["Bucket"], "example-bucket")
        self.assertEqual(call_kwargs["Key"], self.KEY)
        self.assertEqual(call_kwargs["Body"], self.BYTES)
        self.assertEqual(call_kwargs["ContentType"], self.CT)

    def test_returns_none_on_boto3_exception(self):
        # Any boto3 exception must be swallowed -- the handler's caller
        # treats None as "fall back to base64" and continues the job.
        # A regression that re-raised would lose the entire job's outputs.
        mock_boto3 = MagicMock()
        mock_client = MagicMock()
        mock_client.put_object.side_effect = Exception("network error")
        mock_boto3.client.return_value = mock_client

        # boto3 is imported inside the function: inject the mock into
        # sys.modules so `import boto3` resolves to it.
        with patch.dict(sys.modules, {"boto3": mock_boto3}), \
             patch.dict(os.environ, {
                 "R2_ENDPOINT": "https://example.r2.cloudflarestorage.com",
                 "R2_BUCKET": "example-bucket",
                 "R2_ACCESS_KEY_ID": "AKIA-test",
                 "R2_SECRET_ACCESS_KEY": "secret-test",
             }, clear=True):
            result = handler.upload_to_r2(self.BYTES, self.KEY, self.CT)

        self.assertIsNone(result)


class TestTerminalResultManifest(unittest.TestCase):
    def test_key_is_user_isolated_and_rejects_path_injection(self):
        self.assertEqual(
            handler.terminal_manifest_key("user-abc", "prompt_xyz"),
            "temp/user-abc/runpod-results/prompt_xyz.json",
        )
        self.assertIsNone(handler.terminal_manifest_key("../user", "prompt"))
        self.assertIsNone(handler.terminal_manifest_key("user", "prompt/other"))

    @patch("handler.upload_to_r2", return_value="temp/user/runpod-results/prompt.json")
    def test_persists_compact_r2_result(self, upload):
        chunk = {
            "type": "result",
            "images": [{
                "filename": "out.png",
                "type": "r2_key",
                "data": "output/user/prompt/out.png",
            }],
        }

        result = handler.persist_terminal_result("user", "prompt", chunk)

        self.assertEqual(result, "temp/user/runpod-results/prompt.json")
        upload.assert_called_once()
        body, key, content_type = upload.call_args.args
        self.assertEqual(key, "temp/user/runpod-results/prompt.json")
        self.assertEqual(content_type, "application/json")
        decoded = json.loads(body.decode("utf-8"))
        self.assertEqual(decoded["version"], 1)
        self.assertEqual(decoded["promptId"], "prompt")
        self.assertEqual(decoded["result"], chunk)

    @patch("handler.upload_to_r2")
    def test_skips_non_r2_results(self, upload):
        for output_type in ("base64", "s3_url"):
            with self.subTest(output_type=output_type):
                result = handler.persist_terminal_result(
                    "user",
                    "prompt",
                    {
                        "type": "result",
                        "images": [{
                            "filename": "out.png",
                            "type": output_type,
                            "data": "AAAA",
                        }],
                    },
                )
                self.assertIsNone(result)
        upload.assert_not_called()


# ---------------------------------------------------------------------------
# PID file helpers
# ---------------------------------------------------------------------------

class TestPidFile(unittest.TestCase):
    """
    _get_comfyui_pid + _is_comfyui_process_alive gate check_server's
    retry loop. A regression here would cause check_server to either
    hang forever (always returns True) or give up immediately (always
    returns False). Both are bad -- the former stalls every job, the
    latter makes every job fail at the API-availability check.
    """

    def test_get_pid_reads_int(self):
        with patch("builtins.open", mock_open(read_data="12345\n")):
            self.assertEqual(handler._get_comfyui_pid(), 12345)

    def test_get_pid_returns_none_on_missing_file(self):
        with patch("builtins.open", side_effect=FileNotFoundError()):
            self.assertIsNone(handler._get_comfyui_pid())

    def test_get_pid_returns_none_on_garbage(self):
        # A corrupt PID file (e.g. partial write during start.sh) must
        # NOT crash -- treat as "no PID available" and fall through to
        # the fallback retry limit.
        with patch("builtins.open", mock_open(read_data="not-a-number")):
            self.assertIsNone(handler._get_comfyui_pid())

    @patch("handler._get_comfyui_pid", return_value=99999)
    @patch("handler.os.kill")
    def test_process_alive_true(self, mock_kill, _):
        # os.kill(pid, 0) with no exception -> process exists.
        self.assertIs(handler._is_comfyui_process_alive(), True)
        mock_kill.assert_called_once_with(99999, 0)

    @patch("handler._get_comfyui_pid", return_value=99999)
    @patch("handler.os.kill", side_effect=ProcessLookupError)
    def test_process_dead_returns_false(self, mock_kill, _):
        self.assertIs(handler._is_comfyui_process_alive(), False)

    @patch("handler._get_comfyui_pid", return_value=99999)
    @patch("handler.os.kill", side_effect=PermissionError)
    def test_process_no_permission_returns_true(self, mock_kill, _):
        # PermissionError means the process exists but we can't signal
        # it (different UID). For our purposes (liveness check), that's
        # "alive" -- ComfyUI running as root while handler runs as a
        # less-privileged user still means the server may come up.
        self.assertIs(handler._is_comfyui_process_alive(), True)

    @patch("handler._get_comfyui_pid", return_value=None)
    def test_no_pid_file_returns_none(self, _):
        # None is distinct from False: check_server uses None to mean
        # "fall back to the retry limit" rather than "ComfyUI is dead".
        self.assertIsNone(handler._is_comfyui_process_alive())


# ---------------------------------------------------------------------------
# _comfy_server_status
# ---------------------------------------------------------------------------

class TestComfyServerStatus(unittest.TestCase):
    """HTTP reachability probe used by the websocket reconnect path."""

    @patch("handler.requests.get")
    def test_reachable_returns_status(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp

        status = handler._comfy_server_status()
        self.assertTrue(status["reachable"])
        self.assertEqual(status["status_code"], 200)

    @patch("handler.requests.get")
    def test_non_200_returns_reachable_false(self, mock_get):
        # 5xx / 4xx -> reachable=False. status_code surfaced so the caller
        # can log it (helps distinguish "ComfyUI crashed" from "ComfyUI
        # is erroring out").
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_get.return_value = mock_resp

        status = handler._comfy_server_status()
        self.assertFalse(status["reachable"])
        self.assertEqual(status["status_code"], 500)

    @patch("handler.requests.get", side_effect=handler.requests.RequestException("boom"))
    def test_request_exception_returns_error_string(self, _):
        # Any request exception must be caught -- the probe is best-effort.
        # The error string is logged upstream; not raising keeps the
        # reconnect loop alive.
        status = handler._comfy_server_status()
        self.assertFalse(status["reachable"])
        self.assertIn("error", status)
        self.assertIn("boom", status["error"])


# ---------------------------------------------------------------------------
# check_server
# ---------------------------------------------------------------------------

class TestCheckServer(unittest.TestCase):
    """
    check_server is the API-availability gate at handler entry. Its
    contract: return True as soon as ComfyUI responds 200; return False
    if the ComfyUI process dies (PID-aware) OR if no PID file exists
    and the fallback retry limit is hit.
    """

    @patch("handler.requests.get")
    @patch("handler._is_comfyui_process_alive", return_value=None)
    def test_returns_true_on_200(self, _, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp
        self.assertIs(
            handler.check_server("http://127.0.0.1:8188", retries=1, delay=10),
            True,
        )

    @patch("handler.requests.get")
    @patch("handler._is_comfyui_process_alive", return_value=False)
    def test_returns_false_when_process_dead(self, _, mock_get):
        # Even if requests.get would eventually succeed, a dead ComfyUI
        # process means it never will -- fail fast instead of spinning
        # the retry loop. This is the load-bearing reason PID tracking
        # exists: without it, a crashed ComfyUI causes every job to
        # hang for COMFY_API_FALLBACK_MAX_RETRIES x delay.
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp
        self.assertIs(
            handler.check_server("http://127.0.0.1:8188", retries=0, delay=10),
            False,
        )

    @patch("handler.time.sleep")
    @patch("handler.requests.get")
    @patch("handler._is_comfyui_process_alive", return_value=None)
    def test_returns_false_after_fallback_retries(self, _, mock_get, mock_sleep):
        # No PID file -> use fallback retry limit. Each attempt fails with
        # a non-200 -> exhaust retries -> return False. Verifies the
        # fallback path that older deployments without start.sh PID
        # writing still depend on.
        mock_resp = MagicMock()
        mock_resp.status_code = 502
        mock_get.return_value = mock_resp

        result = handler.check_server(
            "http://127.0.0.1:8188", retries=3, delay=10
        )
        self.assertIs(result, False)
        # 3 retries -> 3 requests.get calls (no early exit)
        self.assertEqual(mock_get.call_count, 3)

    @patch("handler.time.sleep")
    @patch("handler.requests.get", side_effect=handler.requests.RequestException)
    @patch("handler._is_comfyui_process_alive", return_value=None)
    def test_swallows_request_exception_and_retries(self, _, mock_get, mock_sleep):
        # Network errors during startup (ComfyUI not listening yet) are
        # expected -- the loop must keep retrying until the process is up
        # or the fallback limit hits.
        result = handler.check_server(
            "http://127.0.0.1:8188", retries=2, delay=10
        )
        self.assertIs(result, False)
        self.assertEqual(mock_get.call_count, 2)


# ---------------------------------------------------------------------------
# _attempt_websocket_reconnect
# ---------------------------------------------------------------------------

class TestWebsocketReconnect(unittest.TestCase):
    """
    Reconnect logic fires when ComfyUI drops the WS mid-job (GPU OOM,
    worker preemption, etc.). The load-bearing contract: if ComfyUI's
    HTTP endpoint is down, do NOT waste reconnect attempts -- bail out
    immediately so the handler can surface a clear "ComfyUI crashed"
    error rather than a misleading "websocket protocol error".
    """

    URL = "ws://127.0.0.1:8188/ws?clientId=test"

    @patch("handler._comfy_server_status")
    def test_aborts_when_comfy_http_down(self, mock_status):
        # ComfyUI HTTP unreachable -> raise immediately. The caller's
        # outer except block catches this and yields a clean error.
        mock_status.return_value = {"reachable": False, "error": "Connection refused"}

        with self.assertRaises(handler.websocket.WebSocketConnectionClosedException):
            handler._attempt_websocket_reconnect(
                self.URL, max_attempts=3, delay_s=0, initial_error=Exception("dropped")
            )
        # Must NOT attempt a reconnect when HTTP is down -- saves the
        # retry budget and produces a clearer error path.
        self.assertEqual(mock_status.call_count, 1)

    @patch("handler.time.sleep")
    @patch("handler._comfy_server_status")
    def test_retries_until_success(self, mock_status, _):
        # HTTP reachable on every probe; first WS connect fails, second
        # succeeds. Verifies the loop returns the new socket and doesn't
        # give up after one failure.
        mock_status.return_value = {"reachable": True, "status_code": 200}

        good_ws = MagicMock()
        with patch("handler.websocket.WebSocket") as mock_ws_class:
            mock_ws_class.return_value = good_ws
            # side_effect: first connect() raises, second succeeds
            good_ws.connect.side_effect = [ConnectionRefusedError, None]

            result = handler._attempt_websocket_reconnect(
                self.URL, max_attempts=3, delay_s=0, initial_error=Exception("dropped")
            )

        self.assertIs(result, good_ws)
        self.assertEqual(good_ws.connect.call_count, 2)

    @patch("handler.time.sleep")
    @patch("handler._comfy_server_status")
    def test_raises_after_max_attempts(self, mock_status, _):
        # HTTP always up, but WS connect always fails -> exhaust attempts
        # and raise. This is the "network glitch that doesn't self-heal"
        # case; the handler's outer except turns this into an error yield.
        mock_status.return_value = {"reachable": True, "status_code": 200}

        with patch("handler.websocket.WebSocket") as mock_ws_class:
            mock_ws = MagicMock()
            mock_ws_class.return_value = mock_ws
            mock_ws.connect.side_effect = websocket_exception_factory()

            with self.assertRaises(handler.websocket.WebSocketConnectionClosedException):
                handler._attempt_websocket_reconnect(
                    self.URL, max_attempts=2, delay_s=0, initial_error=Exception("dropped")
                )

        # 2 attempts -> 2 connect calls (both failed)
        self.assertEqual(mock_ws.connect.call_count, 2)


def websocket_exception_factory():
    """Yield websocket exceptions for side_effect that needs a fresh instance each call."""
    yield handler.websocket.WebSocketException("fail 1")
    yield handler.websocket.WebSocketException("fail 2")


# ---------------------------------------------------------------------------
# upload_images
# ---------------------------------------------------------------------------

class TestUploadImages(unittest.TestCase):
    """upload_images pushes base64-encoded user inputs to ComfyUI's /upload/image."""

    @patch("handler.requests.post")
    def test_success_returns_status_success(self, mock_post):
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.text = "ok"
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        b64 = __import__("base64").b64encode(b"data").decode()
        result = handler.upload_images([{"name": "in.png", "image": b64}])

        self.assertEqual(result["status"], "success")
        self.assertIn("details", result)
        self.assertEqual(len(result["details"]), 1)

    @patch("handler.requests.post")
    def test_4xx_returns_status_error(self, mock_post):
        # ComfyUI returns 400 for oversized payloads / bad filenames.
        # The handler must surface this as an error so the outer flow
        # can fail the job ( uploading inputs is a prerequisite for the
        # workflow -- if it fails there's no point queuing).
        mock_resp = Mock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad Request"
        mock_resp.raise_for_status.side_effect = Exception("400")
        mock_post.return_value = mock_resp

        b64 = __import__("base64").b64encode(b"data").decode()
        result = handler.upload_images([{"name": "in.png", "image": b64}])

        self.assertEqual(result["status"], "error")
        self.assertGreater(len(result["details"]), 0)

    def test_empty_images_short_circuits(self):
        # Empty list -> immediate success, no HTTP call. Avoids a confusing
        # "all 0 images uploaded" log line.
        result = handler.upload_images([])
        self.assertEqual(result["status"], "success")
        self.assertIn("No images", result["message"])

    @patch("handler.requests.post")
    def test_data_uri_prefix_stripped(self, mock_post):
        # Browsers send images as data URIs (data:image/png;base64,XXXX).
        # upload_images must strip the prefix before b64decode, otherwise
        # base64.binascii.Error fires and the upload fails.
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        raw_bytes = b"\x89PNGfake"
        b64 = __import__("base64").b64encode(raw_bytes).decode()
        data_uri = f"data:image/png;base64,{b64}"

        handler.upload_images([{"name": "in.png", "image": data_uri}])

        # The BytesIO passed to requests should contain the decoded bytes.
        _, kwargs = mock_post.call_args
        files = kwargs["files"]
        bytesio = files["image"][1]
        self.assertEqual(bytesio.getvalue(), raw_bytes)


if __name__ == "__main__":
    unittest.main()
