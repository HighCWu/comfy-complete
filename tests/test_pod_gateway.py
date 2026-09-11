"""Unit tests for the Pod gateway's fail-closed authentication boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pod_gateway", ROOT / "docker" / "pod" / "gateway.py"
)
assert SPEC and SPEC.loader
gateway = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gateway)


class PodGatewayTest(unittest.TestCase):
    def test_token_match_is_exact(self) -> None:
        self.assertTrue(gateway.token_matches("secret", "secret"))
        self.assertFalse(gateway.token_matches("secret-x", "secret"))
        self.assertFalse(gateway.token_matches(None, "secret"))

    def test_configured_token_fails_closed(self) -> None:
        previous = os.environ.pop("COMFY_POD_TOKEN", None)
        try:
            with self.assertRaisesRegex(RuntimeError, "COMFY_POD_TOKEN"):
                gateway.configured_token()
        finally:
            if previous is not None:
                os.environ["COMFY_POD_TOKEN"] = previous

    def test_upstream_defaults_to_loopback(self) -> None:
        old_host = os.environ.pop("COMFY_INTERNAL_HOST", None)
        old_port = os.environ.pop("COMFY_INTERNAL_PORT", None)
        try:
            self.assertEqual(gateway.upstream_base_url(), "http://127.0.0.1:8188")
        finally:
            if old_host is not None:
                os.environ["COMFY_INTERNAL_HOST"] = old_host
            if old_port is not None:
                os.environ["COMFY_INTERNAL_PORT"] = old_port

    def test_gateway_secret_is_not_forwarded(self) -> None:
        class Request:
            headers = {
                "Host": "pod.example",
                "Content-Length": "42",
                "X-Comfy-Pod-Token": "secret",
                "X-Trace": "trace-1",
            }

        self.assertEqual(gateway.forwarded_headers(Request()), {"X-Trace": "trace-1"})


class BarrierResult:
    def __init__(
        self,
        *,
        success: bool,
        action: str,
        reason: str = "fixture",
    ) -> None:
        self.success = success
        self.action = action
        self.reason = reason
        self.poll_observations = 1
        self.queue_empty = success
        self.history_empty = success
        self.baseline_ok = success


class FakeBarrier:
    def __init__(self, result: BarrierResult) -> None:
        self.result = result
        self.baselines: list[dict[str, object]] = []

    def reset(self, *, baseline: dict[str, object]) -> BarrierResult:
        self.baselines.append(baseline)
        return self.result


class SlowBarrier:
    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds

    def reset(
        self,
        *,
        baseline: dict[str, object],
        deadline: float,
    ) -> BarrierResult:
        del baseline, deadline
        time.sleep(self.delay_seconds)
        return BarrierResult(success=True, action="ready")


class AsyncSlowBarrier:
    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds

    async def reset(
        self,
        *,
        baseline: dict[str, object],
        deadline: float,
    ) -> BarrierResult:
        del baseline, deadline
        await asyncio.sleep(self.delay_seconds)
        return BarrierResult(success=True, action="ready")


class FakeComfyClient:
    def __init__(self) -> None:
        self.submit_calls: list[tuple[dict[str, object], str]] = []
        self.history_calls: list[str] = []
        self.queue_calls = 0
        self.interrupt_calls: list[str] = []
        self.remove_calls: list[str] = []
        self.history_value: object = {}
        self.queue_value: object = {
            "queue_running": [],
            "queue_pending": [],
        }
        self.submit_error: Exception | None = None
        self.interrupt_error: Exception | None = None
        self.remove_error: Exception | None = None
        self.submit_attempts = 0

    async def submit(
        self,
        workflow: dict[str, object],
        *,
        client_id: str,
    ) -> str:
        self.submit_attempts += 1
        if self.submit_error is not None:
            raise self.submit_error
        self.submit_calls.append((workflow, client_id))
        return "comfy-prompt-1"

    async def history(self, prompt_id: str) -> object:
        self.history_calls.append(prompt_id)
        return self.history_value

    async def queue(self) -> object:
        self.queue_calls += 1
        return self.queue_value

    async def interrupt(self, prompt_id: str) -> None:
        if self.interrupt_error is not None:
            raise self.interrupt_error
        self.interrupt_calls.append(prompt_id)

    async def remove_from_queue(self, prompt_id: str) -> None:
        if self.remove_error is not None:
            raise self.remove_error
        self.remove_calls.append(prompt_id)


class FakeHTTPResponse:
    def __init__(
        self,
        status: int,
        body: bytes,
        *,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._body = body
        self._chunks = list(chunks) if chunks is not None else None

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if self._chunks is not None:
            return self._chunks.pop(0) if self._chunks else b""
        if size < 0:
            body, self._body = self._body, b""
            return body
        body, self._body = self._body[:size], self._body[size:]
        return body

    def close(self) -> None:
        return None


class ComfyHTTPTransportTest(unittest.IsolatedAsyncioTestCase):
    def test_declared_response_limit_fails_before_reading_body(self) -> None:
        response = FakeHTTPResponse(
            200,
            b"secret-body",
            headers={"Content-Length": str(gateway.MAX_COMFY_RESPONSE_BYTES + 1)},
        )
        with patch.object(gateway.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "too large") as raised:
                gateway.ComfyHTTPTransport("http://127.0.0.1:8188").request(
                    "GET",
                    "/history/prompt",
                    timeout_seconds=1.0,
                )
        self.assertNotIn("secret-body", str(raised.exception))

    def test_streamed_response_limit_is_bounded_without_content_length(self) -> None:
        response = FakeHTTPResponse(
            200,
            b"",
            chunks=[
                b"a" * gateway.MAX_COMFY_RESPONSE_BYTES,
                b"secret-overflow",
            ],
        )
        with patch.object(gateway.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "too large") as raised:
                gateway.ComfyHTTPTransport("http://127.0.0.1:8188").request(
                    "GET",
                    "/history/prompt",
                    timeout_seconds=1.0,
                )
        self.assertNotIn("secret-overflow", str(raised.exception))

    async def test_http_client_distinguishes_404_from_definitive_submit_rejection(self) -> None:
        transport = gateway.ComfyHTTPTransport("http://127.0.0.1:8188")
        client = gateway.ComfyHTTPExecutionClient()
        client._transport = transport
        with patch.object(
            gateway.urllib.request,
            "urlopen",
            return_value=FakeHTTPResponse(404, b"not found"),
        ):
            self.assertEqual(await client.history("prompt"), {})

        with patch.object(
            gateway.urllib.request,
            "urlopen",
            return_value=FakeHTTPResponse(500, b'{"secret":"do-not-leak"}'),
        ):
            with self.assertRaises(gateway.ComfySubmissionRejected) as raised:
                await client.submit({"3": {}}, client_id="worker-test")
        self.assertNotIn("do-not-leak", str(raised.exception))

    async def test_json_body_stops_incremental_read_at_request_limit(self) -> None:
        class Content:
            def __init__(self) -> None:
                self.chunks = [
                    b"a" * (gateway.MAX_WORKER_REQUEST_BYTES - 1),
                    b"overflow",
                    b"must-not-be-read",
                ]
                self.read_count = 0

            async def iter_chunked(self, _size: int):
                for chunk in self.chunks:
                    self.read_count += 1
                    yield chunk

        class Request:
            content_length = None

            def __init__(self, content: Content) -> None:
                self.content = content

        content = Content()
        with self.assertRaises(gateway.WorkerRequestTooLarge):
            await gateway._json_body(Request(content))
        self.assertEqual(content.read_count, 2)


class WorkerProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def _client(
        self,
        *,
        capability: str | None,
        barrier: FakeBarrier | None = None,
        restart_callback: object | None = None,
        restart_probe: object | None = None,
        restart_generation_path: str | None = None,
        worker_instance_root: str | None = None,
        comfy_client: FakeComfyClient | None = None,
        now_fn: Callable[[], int] | None = None,
    ) -> tuple[TestClient, TestServer]:
        instance_root = Path(
            worker_instance_root or "/tmp/comfy-runtime/inst_gatewaytest"
        )
        shutil.rmtree(instance_root, ignore_errors=True)
        instance_root.mkdir(parents=True, exist_ok=True)
        app = gateway.create_app(
            token="browser-token",
            worker_capability_secret=capability,
            worker_id="worker-test",
            worker_initial_state="ready",
            worker_baseline={"system": {}},
            now_fn=now_fn or (lambda: 1_000),
            reset_barrier=barrier,
            restart_callback=restart_callback,
            restart_probe=restart_probe,
            restart_generation_path=restart_generation_path,
            worker_instance_root=str(instance_root),
            comfy_client=comfy_client,
        )
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        return client, server

    async def _finish_successful_execution(
        self,
        client: TestClient,
        capability_headers: dict[str, str],
        claim_payload: dict[str, object],
        assignment_token: str,
        comfy: FakeComfyClient,
    ) -> None:
        execution_payload = {
            **claim_payload,
            "execution_id": "execution-a",
        }
        response = await client.post(
            "/__worker/execute",
            headers={
                **capability_headers,
                gateway.ASSIGNMENT_TOKEN_HEADER: assignment_token,
            },
            json={
                **execution_payload,
                "workflow": {"3": {"class_type": "Test"}},
            },
        )
        self.assertEqual(response.status, 200)
        comfy.history_value = {
            "comfy-prompt-1": {
                "status": {"status_str": "success", "completed": True},
                "outputs": {},
            }
        }
        response = await client.post(
            "/__worker/result",
            headers={
                **capability_headers,
                gateway.ASSIGNMENT_TOKEN_HEADER: assignment_token,
            },
            json=execution_payload,
        )
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["kind"], "completed")

    async def test_starting_worker_captures_baseline_before_advertising_ready(self) -> None:
        app = gateway.create_app(
            token="browser-token",
            worker_capability_secret="worker-cap",
            worker_id="worker-starting",
            baseline_probe=lambda: {"system": {"ram_total": 4}},
        )
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            response = await client.get(
                "/__worker/status",
                headers={gateway.WORKER_CAPABILITY_HEADER: "worker-cap"},
            )
            self.assertEqual(response.status, 200)
            payload = await response.json()
            self.assertEqual(payload["state"], "ready")
            self.assertTrue(payload["baseline_configured"])
            self.assertTrue(payload["ready"])
        finally:
            await client.close()

    async def test_reset_deadline_quarantines_a_late_barrier_result(self) -> None:
        instance_root = Path("/tmp/comfy-runtime/inst_timeouttest")
        shutil.rmtree(instance_root, ignore_errors=True)
        instance_root.mkdir(parents=True)
        try:
            controller = gateway.WorkerController(
                "worker-timeout",
                initial_state="ready",
                baseline={"system": {}},
                reset_barrier=SlowBarrier(0.1),
                instance_root=str(instance_root),
                reset_timeout_seconds=0.02,
            )
            identity = {
                "assignment_id": "assignment-timeout",
                "assignment_token": "assignment-secret",
                "user_id": "user-a",
                "workspace_id": "workspace-a",
                "job_id": "job-a",
                "affinity_key": "plan-a",
            }
            status, _ = await controller.claim(identity)
            self.assertEqual(status, 200)
            status, _ = await controller.complete(
                {**identity, "status": "failed"},
                "assignment-secret",
            )
            self.assertEqual(status, 200)

            started = time.monotonic()
            status, payload = await controller.reset(
                identity,
                "assignment-secret",
            )
            elapsed = time.monotonic() - started
            self.assertEqual(status, 503)
            self.assertLess(elapsed, 0.08)
            self.assertEqual(payload["state"], "error")
            self.assertFalse(payload["ready"])

            # The worker thread cannot be forcibly killed, so prove that its
            # late success cannot promote the controller back to reusable.
            await asyncio.sleep(0.12)
            self.assertEqual(controller.state, "error")
            self.assertIsNotNone(controller.status_payload()["assignment"])
        finally:
            shutil.rmtree(instance_root, ignore_errors=True)

    async def test_reset_deadline_bounds_async_barrier_result(self) -> None:
        instance_root = Path("/tmp/comfy-runtime/inst_asynctimeouttest")
        shutil.rmtree(instance_root, ignore_errors=True)
        instance_root.mkdir(parents=True)
        try:
            controller = gateway.WorkerController(
                "worker-async-timeout",
                initial_state="ready",
                baseline={"system": {}},
                reset_barrier=AsyncSlowBarrier(0.1),
                instance_root=str(instance_root),
                reset_timeout_seconds=0.02,
            )
            identity = {
                "assignment_id": "assignment-async-timeout",
                "assignment_token": "assignment-secret",
                "user_id": "user-a",
                "workspace_id": "workspace-a",
                "job_id": "job-a",
                "affinity_key": "plan-a",
            }
            status, _ = await controller.claim(identity)
            self.assertEqual(status, 200)
            status, _ = await controller.complete(
                {**identity, "status": "failed"},
                "assignment-secret",
            )
            self.assertEqual(status, 200)

            started = time.monotonic()
            status, payload = await controller.reset(
                identity,
                "assignment-secret",
            )
            elapsed = time.monotonic() - started
            self.assertEqual(status, 503)
            self.assertLess(elapsed, 0.08)
            self.assertEqual(payload["state"], "error")
            self.assertFalse(payload["ready"])
            self.assertIsNotNone(controller.status_payload()["assignment"])
        finally:
            shutil.rmtree(instance_root, ignore_errors=True)

    async def test_capability_is_required_and_disabled_namespace_is_not_proxied(self) -> None:
        client, _server = await self._client(capability=None)
        try:
            response = await client.get(
                "/__worker/status",
                headers={gateway.TOKEN_HEADER: "browser-token"},
            )
            self.assertEqual(response.status, 404)
        finally:
            await client.close()

    async def test_legacy_rpcs_require_exact_identity_and_reject_alias_or_unknown_fields(self) -> None:
        client, _server = await self._client(capability="worker-cap")
        headers = {gateway.WORKER_CAPABILITY_HEADER: "worker-cap"}
        identity = {
            "user_id": "user-a",
            "workspace_id": "workspace-a",
            "assignment_id": "assignment-a",
            "assignment_token": "assignment-secret",
            "job_id": "job-a",
            "affinity_key": "plan-a",
        }
        try:
            response = await client.post(
                "/__worker/claim",
                headers=headers,
                json={**identity, "assignmentId": "other-assignment"},
            )
            self.assertEqual(response.status, 400)

            response = await client.post(
                "/__worker/claim",
                headers=headers,
                json={**identity, "unexpected": True},
            )
            self.assertEqual(response.status, 400)

            response = await client.post("/__worker/claim", headers=headers, json=identity)
            self.assertEqual(response.status, 200)

            response = await client.post(
                "/__worker/heartbeat",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json={key: value for key, value in identity.items() if key != "assignment_id"},
            )
            self.assertEqual(response.status, 409)

            response = await client.post(
                "/__worker/heartbeat",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "wrong-secret",
                },
                json=identity,
            )
            self.assertEqual(response.status, 401)

            for path, payload in (
                ("/__worker/heartbeat", {**identity, "unexpected": True}),
                ("/__worker/complete", {**identity, "unexpected": True}),
                ("/__worker/reset", {**identity, "unexpected": True}),
            ):
                response = await client.post(
                    path,
                    headers={
                        **headers,
                        gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                    },
                    json=payload,
                )
                self.assertEqual(response.status, 400)
        finally:
            await client.close()

        client, _server = await self._client(capability="worker-cap")
        try:
            response = await client.get("/__worker/status")
            self.assertEqual(response.status, 401)
            response = await client.get(
                "/__worker/status",
                headers={gateway.WORKER_CAPABILITY_HEADER: "worker-cap"},
            )
            self.assertEqual(response.status, 200)
            payload = await response.json()
            self.assertEqual(payload["state"], "ready")
            self.assertTrue(payload["ready"])
        finally:
            await client.close()

    async def test_claim_heartbeat_complete_and_cross_user_reset(self) -> None:
        barrier = FakeBarrier(BarrierResult(success=True, action="ready"))
        comfy = FakeComfyClient()
        client, _server = await self._client(
            capability="worker-cap",
            barrier=barrier,
            comfy_client=comfy,
        )
        headers = {gateway.WORKER_CAPABILITY_HEADER: "worker-cap"}
        identity = {
            "user_id": "user-a",
            "workspace_id": "workspace-a",
        }
        try:
            # The control plane owns the assignment capability. Requiring it
            # makes a lost claim response safely retryable without relying on
            # worker-local random state that the caller never observed.
            response = await client.post(
                "/__worker/claim",
                headers=headers,
                json={
                    **identity,
                    "assignment_id": "assignment-a",
                    "job_id": "job-a",
                    "affinity_key": "plan-a",
                },
            )
            self.assertEqual(response.status, 400)

            claim_payload = {
                **identity,
                "assignment_id": "assignment-a",
                "assignment_token": "token-assignment-a",
                "job_id": "job-a",
                "affinity_key": "plan-a",
            }
            response = await client.post(
                "/__worker/claim",
                headers=headers,
                json=claim_payload,
            )
            self.assertEqual(response.status, 200)
            claimed = await response.json()
            self.assertEqual(claimed["state"], "busy")
            self.assertNotIn("assignment_token", claimed)
            assignment_token = claim_payload["assignment_token"]

            response = await client.post(
                "/__worker/claim",
                headers=headers,
                json=claim_payload,
            )
            self.assertEqual(response.status, 200)
            duplicate_claim = await response.json()
            self.assertEqual(duplicate_claim["kind"], "duplicate")
            self.assertNotIn("assignment_token", duplicate_claim)

            response = await client.get("/__worker/status", headers=headers)
            status = await response.json()
            self.assertNotIn("assignment_token", status["assignment"])
            self.assertEqual(status["assignment"]["affinity_key"], "plan-a")

            response = await client.post(
                "/__worker/heartbeat",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "wrong-token",
                },
                json=claim_payload,
            )
            self.assertEqual(response.status, 401)

            response = await client.post(
                "/__worker/heartbeat",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: assignment_token,
                },
                json={**claim_payload, "workspace_id": "other-workspace"},
            )
            self.assertEqual(response.status, 409)

            response = await client.post(
                "/__worker/heartbeat",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: assignment_token,
                },
                json=claim_payload,
            )
            self.assertEqual(response.status, 200)
            heartbeat = await response.json()
            self.assertEqual(heartbeat["assignment"]["status"], "running")

            await self._finish_successful_execution(
                client,
                headers,
                claim_payload,
                assignment_token,
                comfy,
            )

            response = await client.post(
                "/__worker/complete",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: assignment_token,
                },
                json={
                    **claim_payload,
                    "status": "completed",
                    "keep_warm": True,
                    "warm_until": 2_000,
                },
            )
            self.assertEqual(response.status, 200)
            completed = await response.json()
            self.assertEqual(completed["state"], "user_warm")
            self.assertFalse(completed["ready"])
            response = await client.post(
                "/__worker/complete",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: assignment_token,
                },
                json={
                    **claim_payload,
                    "status": "completed",
                    "keep_warm": True,
                    "warm_until": 2_000,
                },
            )
            self.assertEqual(response.status, 200)
            duplicate_complete = await response.json()
            self.assertEqual(duplicate_complete["kind"], "duplicate")
            self.assertFalse(duplicate_complete["reset_required"])

            instance_root = Path("/tmp/comfy-runtime/inst_gatewaytest")
            for name in ("input", "output", "temp", "user"):
                (instance_root / name).mkdir(parents=True, exist_ok=True)
            (instance_root / "input" / "private.bin").write_bytes(b"private")
            (instance_root / "output" / "result.png").write_bytes(b"result")
            (instance_root / "temp" / "scratch").write_bytes(b"scratch")
            (instance_root / "user" / "history.json").write_bytes(b"history")
            (instance_root / "cache").mkdir(parents=True, exist_ok=True)
            (instance_root / "cache" / "keep.bin").write_bytes(b"keep")

            response = await client.post(
                "/__worker/reset",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: assignment_token,
                },
                json={**claim_payload, "baseline": {"arbitrary": "input"}},
            )
            self.assertEqual(response.status, 400)

            response = await client.post(
                "/__worker/reset",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: assignment_token,
                },
                json=claim_payload,
            )
            self.assertEqual(response.status, 200)
            reset = await response.json()
            self.assertEqual(reset["state"], "ready")
            self.assertTrue(reset["ready"])
            self.assertIsNone(reset["assignment"])
            self.assertEqual(barrier.baselines, [{"system": {}}])
            for name in ("input", "output", "temp", "user"):
                child = instance_root / name
                self.assertTrue(child.is_dir())
                self.assertEqual(list(child.iterdir()), [])
            self.assertEqual(
                (instance_root / "cache" / "keep.bin").read_bytes(), b"keep"
            )

            response = await client.post(
                "/__worker/claim",
                headers=headers,
                json={
                    "user_id": "user-b",
                    "workspace_id": "workspace-b",
                    "assignment_id": "assignment-b",
                    "assignment_token": "token-assignment-b",
                    "job_id": "job-b",
                    "affinity_key": "plan-b",
                },
            )
            self.assertEqual(response.status, 200)
        finally:
            await client.close()

    async def test_same_user_warm_reuse_requires_matching_plan_and_future_expiry(self) -> None:
        now = [1_000]
        comfy = FakeComfyClient()
        client, _server = await self._client(
            capability="worker-cap",
            now_fn=lambda: now[0],
            comfy_client=comfy,
        )
        headers = {gateway.WORKER_CAPABILITY_HEADER: "worker-cap"}
        identity = {
            "user_id": "user-a",
            "workspace_id": "workspace-a",
        }
        try:
            response = await client.post(
                "/__worker/claim",
                headers=headers,
                json={
                    **identity,
                    "assignment_id": "assignment-a",
                    "assignment_token": "token-assignment-a",
                    "job_id": "job-a",
                    "affinity_key": "plan-a",
                },
            )
            self.assertEqual(response.status, 200)
            assignment_token = "token-assignment-a"
            claim_identity = {
                **identity,
                "assignment_id": "assignment-a",
                "assignment_token": assignment_token,
                "job_id": "job-a",
                "affinity_key": "plan-a",
            }
            await self._finish_successful_execution(
                client,
                headers,
                claim_identity,
                assignment_token,
                comfy,
            )
            response = await client.post(
                "/__worker/complete",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: assignment_token,
                },
                json={
                    **claim_identity,
                    "status": "completed",
                    "keep_warm": True,
                    "warm_until": 2_000,
                },
            )
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["state"], "user_warm")

            # A different model plan cannot reuse the retained process state.
            response = await client.post(
                "/__worker/claim",
                headers=headers,
                json={
                    **identity,
                    "assignment_id": "assignment-b-wrong-plan",
                    "assignment_token": "token-assignment-b-wrong-plan",
                    "job_id": "job-b-wrong-plan",
                    "affinity_key": "plan-b",
                },
            )
            self.assertEqual(response.status, 409)

            response = await client.post(
                "/__worker/claim",
                headers=headers,
                json={
                    **identity,
                    "assignment_id": "assignment-b",
                    "assignment_token": "token-assignment-b",
                    "job_id": "job-b",
                    "affinity_key": "plan-a",
                },
            )
            self.assertEqual(response.status, 200)
            reused = await response.json()
            self.assertTrue(reused["warm_reused"])
            self.assertEqual(reused["assignment"]["assignment_id"], "assignment-b")
            self.assertEqual(reused["assignment"]["affinity_key"], "plan-a")

            token_b = "token-assignment-b"
            claim_identity_b = {
                **identity,
                "assignment_id": "assignment-b",
                "assignment_token": token_b,
                "job_id": "job-b",
                "affinity_key": "plan-a",
            }
            await self._finish_successful_execution(
                client,
                headers,
                claim_identity_b,
                token_b,
                comfy,
            )
            response = await client.post(
                "/__worker/complete",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: token_b,
                },
                json={
                    **claim_identity_b,
                    "status": "completed",
                    "keep_warm": True,
                    "warm_until": 3_000,
                },
            )
            self.assertEqual(response.status, 200)

            # An expired USER_WARM assignment must be reset, not reused.
            now[0] = 3_000
            response = await client.post(
                "/__worker/claim",
                headers=headers,
                json={
                    **identity,
                    "assignment_id": "assignment-c",
                    "assignment_token": "token-assignment-c",
                    "job_id": "job-c",
                    "affinity_key": "plan-a",
                },
            )
            self.assertEqual(response.status, 409)
        finally:
            await client.close()

    async def test_single_task_concurrency_and_assignment_replay_are_bounded(self) -> None:
        client, _server = await self._client(capability="worker-cap")
        headers = {gateway.WORKER_CAPABILITY_HEADER: "worker-cap"}
        try:
            async def claim(assignment_id: str) -> int:
                response = await client.post(
                    "/__worker/claim",
                    headers=headers,
                    json={
                        "user_id": "user-a",
                        "workspace_id": "workspace-a",
                        "assignment_id": assignment_id,
                        "job_id": f"job-{assignment_id}",
                        "affinity_key": "plan-a",
                        "assignment_token": f"token-{assignment_id}",
                    },
                )
                await response.read()
                return response.status

            statuses = await asyncio.gather(claim("assignment-a"), claim("assignment-b"))
            self.assertEqual(sorted(statuses), [200, 409])
        finally:
            await client.close()

    async def test_execute_is_assignment_bound_and_replay_does_not_resubmit(self) -> None:
        comfy = FakeComfyClient()
        client, _server = await self._client(
            capability="worker-cap",
            comfy_client=comfy,
        )
        headers = {gateway.WORKER_CAPABILITY_HEADER: "worker-cap"}
        identity = {
            "user_id": "user-a",
            "workspace_id": "workspace-a",
            "assignment_id": "assignment-a",
            "job_id": "job-a",
            "affinity_key": "plan-a",
            "assignment_token": "assignment-secret",
        }
        workflow = {"3": {"class_type": "EmptyLatentImage", "inputs": {}}}
        try:
            response = await client.post("/__worker/claim", headers=headers, json=identity)
            self.assertEqual(response.status, 200)

            execute = {
                **identity,
                "execution_id": "execution-a",
                "workflow": workflow,
            }
            response = await client.post(
                "/__worker/execute",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json=execute,
            )
            self.assertEqual(response.status, 200)
            submitted = await response.json()
            self.assertEqual(submitted["kind"], "accepted")
            self.assertEqual(submitted["execution"]["status"], "queued")
            self.assertIsNone(submitted["execution"]["result"])
            self.assertNotIn("assignment-secret", await response.text())
            self.assertEqual(len(comfy.submit_calls), 1)
            self.assertTrue(comfy.submit_calls[0][1].startswith("worker-"))

            response = await client.post(
                "/__worker/execute",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json=execute,
            )
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["kind"], "duplicate")
            self.assertEqual(len(comfy.submit_calls), 1)

            response = await client.post(
                "/__worker/execute",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "wrong-secret",
                },
                json=execute,
            )
            self.assertEqual(response.status, 401)

            response = await client.post(
                "/__worker/execute",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json={**execute, "job_id": "other-job"},
            )
            self.assertEqual(response.status, 409)

            response = await client.post(
                "/__worker/execute",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json={**execute, "workflow": {"different": True}},
            )
            self.assertEqual(response.status, 409)
        finally:
            await client.close()

    async def test_concurrent_execute_replay_submits_only_once(self) -> None:
        comfy = FakeComfyClient()
        client, _server = await self._client(
            capability="worker-cap",
            comfy_client=comfy,
        )
        headers = {gateway.WORKER_CAPABILITY_HEADER: "worker-cap"}
        identity = {
            "user_id": "user-a",
            "workspace_id": "workspace-a",
            "assignment_id": "assignment-a",
            "job_id": "job-a",
            "affinity_key": "plan-a",
            "assignment_token": "assignment-secret",
            "execution_id": "execution-a",
            "workflow": {"3": {"class_type": "Test"}},
        }
        claim_identity = {
            key: value
            for key, value in identity.items()
            if key not in {"execution_id", "workflow"}
        }
        try:
            response = await client.post(
                "/__worker/claim", headers=headers, json=claim_identity
            )
            self.assertEqual(response.status, 200)

            async def execute() -> tuple[int, str]:
                result = await client.post(
                    "/__worker/execute",
                    headers={
                        **headers,
                        gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                    },
                    json=identity,
                )
                body = await result.json()
                return result.status, body["kind"]

            responses = await asyncio.gather(execute(), execute())
            self.assertEqual(sorted(responses), [(200, "accepted"), (200, "duplicate")])
            self.assertEqual(len(comfy.submit_calls), 1)
        finally:
            await client.close()

    async def test_submission_unknown_isolated_until_reset_and_never_resubmitted(self) -> None:
        barrier = FakeBarrier(BarrierResult(success=True, action="ready"))
        comfy = FakeComfyClient()
        comfy.submit_error = TimeoutError("/secret/provider/path")
        client, _server = await self._client(
            capability="worker-cap",
            barrier=barrier,
            comfy_client=comfy,
        )
        headers = {gateway.WORKER_CAPABILITY_HEADER: "worker-cap"}
        identity = {
            "user_id": "user-a",
            "workspace_id": "workspace-a",
            "assignment_id": "assignment-a",
            "job_id": "job-a",
            "affinity_key": "plan-a",
            "assignment_token": "assignment-secret",
        }
        execute = {
            **identity,
            "execution_id": "execution-a",
            "workflow": {"3": {"class_type": "Test"}},
        }
        try:
            response = await client.post("/__worker/claim", headers=headers, json=identity)
            self.assertEqual(response.status, 200)

            response = await client.post(
                "/__worker/execute",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json=execute,
            )
            self.assertEqual(response.status, 502)
            first = await response.json()
            self.assertEqual(first["execution"]["status"], "queued")
            self.assertEqual(first["execution"]["error_code"], "COMFY_SUBMIT_UNKNOWN")
            self.assertNotIn("/secret/provider/path", await response.text())
            self.assertEqual(comfy.submit_attempts, 1)

            response = await client.post(
                "/__worker/execute",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json=execute,
            )
            self.assertEqual(response.status, 502)
            self.assertEqual((await response.json())["kind"], "duplicate")
            self.assertEqual(comfy.submit_attempts, 1)

            response = await client.post(
                "/__worker/complete",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json={**identity, "status": "failed"},
            )
            self.assertEqual(response.status, 409)

            response = await client.post(
                "/__worker/reset",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json=identity,
            )
            self.assertEqual(response.status, 200)
            reset = await response.json()
            self.assertEqual(reset["state"], "ready")
            self.assertIsNone(reset["assignment"])
        finally:
            await client.close()

    async def test_submission_unknown_reset_failure_is_idempotent(self) -> None:
        barrier = FakeBarrier(
            BarrierResult(
                success=False,
                action="request_restart",
                reason="secret provider reset detail",
            )
        )
        comfy = FakeComfyClient()
        comfy.submit_error = TimeoutError("secret submit detail")
        client, _server = await self._client(
            capability="worker-cap",
            barrier=barrier,
            comfy_client=comfy,
            restart_callback=lambda: False,
        )
        headers = {gateway.WORKER_CAPABILITY_HEADER: "worker-cap"}
        identity = {
            "user_id": "user-a",
            "workspace_id": "workspace-a",
            "assignment_id": "assignment-a",
            "job_id": "job-a",
            "affinity_key": "plan-a",
            "assignment_token": "assignment-secret",
        }
        execute = {
            **identity,
            "execution_id": "execution-a",
            "workflow": {"3": {"class_type": "Test"}},
        }
        try:
            response = await client.post("/__worker/claim", headers=headers, json=identity)
            self.assertEqual(response.status, 200)
            response = await client.post(
                "/__worker/execute",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json=execute,
            )
            self.assertEqual(response.status, 502)

            reset_payload = {
                **identity,
            }
            response = await client.post(
                "/__worker/reset",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json=reset_payload,
            )
            self.assertEqual(response.status, 503)
            first = await response.json()
            self.assertEqual(first["error_code"], "WORKER_RESTART_FAILED")
            self.assertEqual(first["error"], "worker reset failed")
            self.assertNotIn("secret provider reset detail", await response.text())

            response = await client.post(
                "/__worker/reset",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json=reset_payload,
            )
            self.assertEqual(response.status, 503)
            second = await response.json()
            self.assertEqual(second["error_code"], "RESET_BARRIER_NOT_CLEAN")
            self.assertEqual(second["error"], "worker reset failed")
            self.assertNotIn("secret provider reset detail", await response.text())
        finally:
            await client.close()

    async def test_result_returns_pending_and_restricted_terminal_outputs(self) -> None:
        comfy = FakeComfyClient()
        client, _server = await self._client(
            capability="worker-cap",
            comfy_client=comfy,
        )
        headers = {gateway.WORKER_CAPABILITY_HEADER: "worker-cap"}
        identity = {
            "user_id": "user-a",
            "workspace_id": "workspace-a",
            "assignment_id": "assignment-a",
            "job_id": "job-a",
            "affinity_key": "plan-a",
            "assignment_token": "assignment-secret",
            "execution_id": "execution-a",
        }
        claim_identity = {
            key: value for key, value in identity.items() if key != "execution_id"
        }
        try:
            response = await client.post(
                "/__worker/claim", headers=headers, json=claim_identity
            )
            self.assertEqual(response.status, 200)
            response = await client.post(
                "/__worker/execute",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json={**identity, "workflow": {"3": {"class_type": "Test"}}},
            )
            self.assertEqual(response.status, 200)

            comfy.history_value = {}
            response = await client.post(
                "/__worker/result",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json=identity,
            )
            self.assertEqual(response.status, 200)
            pending = await response.json()
            self.assertEqual(pending["kind"], "pending")
            self.assertEqual(pending["execution"]["status"], "queued")
            self.assertIsNone(pending["execution"]["result"])

            instance_root = Path("/tmp/comfy-runtime/inst_gatewaytest")
            output_subfolder = instance_root / "output" / "job-a"
            output_subfolder.mkdir(parents=True, exist_ok=True)
            image_bytes = b"png-result"
            audio_bytes = b"wav-result"
            (output_subfolder / "result.png").write_bytes(image_bytes)
            (instance_root / "output" / "result.wav").write_bytes(audio_bytes)

            comfy.history_value = {
                "comfy-prompt-1": {
                    "status": {
                        "status_str": "success",
                        "completed": True,
                    },
                    "outputs": {
                        "7": {
                            "images": [
                                {
                                    "filename": "result.png",
                                    "subfolder": "job-a",
                                    "type": "output",
                                    "secret_metadata": "must-not-escape",
                                }
                            ],
                            "audio": [
                                {
                                    "filename": "result.wav",
                                    "subfolder": "",
                                    "type": "output",
                                }
                            ],
                            "files": [
                                {
                                    "filename": "private.bin",
                                    "type": "input",
                                },
                                {
                                    "filename": "scratch.bin",
                                    "type": "temp",
                                },
                            ],
                            "arbitrary": [{"secret": "drop"}],
                        }
                    },
                    "prompt": {"secret_workflow": True},
                }
            }
            response = await client.post(
                "/__worker/result",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json=identity,
            )
            self.assertEqual(response.status, 200)
            result = await response.json()
            self.assertEqual(result["kind"], "completed")
            self.assertEqual(result["execution"]["status"], "completed")
            outputs = result["execution"]["result"]["outputs"]
            self.assertEqual(set(outputs), {"7"})
            self.assertNotIn("files", outputs["7"])
            image = outputs["7"]["images"][0]
            self.assertEqual(image["filename"], "result.png")
            self.assertEqual(image["subfolder"], "job-a")
            self.assertEqual(image["type"], "output")
            self.assertEqual(image["category"], "images")
            self.assertEqual(image["size_bytes"], len(image_bytes))
            self.assertEqual(
                image["sha256"], hashlib.sha256(image_bytes).hexdigest()
            )
            self.assertEqual(image["content_type"], "image/png")
            self.assertRegex(image["output_id"], r"^[0-9a-f]{64}$")
            audio = outputs["7"]["audio"][0]
            self.assertEqual(audio["filename"], "result.wav")
            self.assertEqual(audio["subfolder"], "")
            self.assertEqual(audio["type"], "output")
            self.assertEqual(audio["category"], "audio")
            self.assertEqual(audio["size_bytes"], len(audio_bytes))
            self.assertEqual(
                audio["sha256"], hashlib.sha256(audio_bytes).hexdigest()
            )
            self.assertEqual(audio["content_type"], "audio/x-wav")
            self.assertNotIn("secret_workflow", await response.text())
            self.assertNotIn("assignment-secret", await response.text())

            history_calls = len(comfy.history_calls)
            response = await client.post(
                "/__worker/result",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json=identity,
            )
            self.assertEqual(response.status, 200)
            self.assertEqual(len(comfy.history_calls), history_calls)
            replay = await response.json()
            self.assertEqual(
                replay["execution"]["result"]["outputs"],
                outputs,
            )

            image_query = (
                "assignment_id=assignment-a&user_id=user-a&workspace_id=workspace-a"
                "&job_id=job-a&affinity_key=plan-a&execution_id=execution-a"
                f"&output_id={image['output_id']}"
            )
            response = await client.get(
                f"/__worker/output?{image_query}",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
            )
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.read(), image_bytes)
            self.assertEqual(response.headers["Content-Length"], str(len(image_bytes)))
            self.assertEqual(response.headers["Content-Type"], "image/png")
            self.assertEqual(response.headers["X-Output-ID"], image["output_id"])
            self.assertEqual(response.headers["X-Output-SHA256"], image["sha256"])
        finally:
            await client.close()

    async def test_output_download_rejects_wrong_identity_and_unsafe_files(self) -> None:
        comfy = FakeComfyClient()
        client, _server = await self._client(
            capability="worker-cap",
            comfy_client=comfy,
        )
        headers = {gateway.WORKER_CAPABILITY_HEADER: "worker-cap"}
        identity = {
            "user_id": "user-a",
            "workspace_id": "workspace-a",
            "assignment_id": "assignment-a",
            "job_id": "job-a",
            "affinity_key": "plan-a",
            "assignment_token": "assignment-secret",
            "execution_id": "execution-a",
        }
        claim_identity = {
            key: value for key, value in identity.items() if key != "execution_id"
        }
        output_root = Path("/tmp/comfy-runtime/inst_gatewaytest/output")
        output_path = output_root / "result.bin"
        outside_path = Path("/tmp/comfy-runtime/output-outside.bin")
        try:
            response = await client.post(
                "/__worker/claim", headers=headers, json=claim_identity
            )
            self.assertEqual(response.status, 200)
            response = await client.post(
                "/__worker/execute",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json={**identity, "workflow": {"3": {"class_type": "Test"}}},
            )
            self.assertEqual(response.status, 200)
            output_bytes = b"private-output"
            output_root.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(output_bytes)
            comfy.history_value = {
                "comfy-prompt-1": {
                    "status": {"status_str": "success", "completed": True},
                    "outputs": {
                        "3": {
                            "files": [
                                {"filename": "result.bin", "type": "output"}
                            ]
                        }
                    },
                }
            }
            response = await client.post(
                "/__worker/result",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json=identity,
            )
            self.assertEqual(response.status, 200)
            output_id = (await response.json())["execution"]["result"]["outputs"]["3"]["files"][0]["output_id"]
            query = (
                "assignment_id=assignment-a&user_id=user-a&workspace_id=workspace-a"
                "&job_id=job-a&affinity_key=plan-a&execution_id=execution-a"
                f"&output_id={output_id}"
            )
            good_headers = {
                **headers,
                gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
            }

            response = await client.get(
                f"/__worker/output?{query}", headers=headers
            )
            self.assertEqual(response.status, 401)
            response = await client.get(
                f"/__worker/output?{query}",
                headers={
                    **headers,
                    "X-Comfy-Assignment-Token": "assignment-secret",
                },
            )
            self.assertEqual(response.status, 401)
            response = await client.get(
                f"/__worker/output?{query}",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "wrong-token",
                },
            )
            self.assertEqual(response.status, 401)
            response = await client.get(
                f"/__worker/output?{query.replace('user-a', 'user-b')}",
                headers=good_headers,
            )
            self.assertEqual(response.status, 409)
            response = await client.get(
                f"/__worker/output?{query.replace('execution-a', 'execution-b')}",
                headers=good_headers,
            )
            self.assertEqual(response.status, 404)
            response = await client.get(
                f"/__worker/output?{query.replace(output_id, '0' * 64)}",
                headers=good_headers,
            )
            self.assertEqual(response.status, 404)
            response = await client.get(
                f"/__worker/output?{query}&path=../result.bin",
                headers=good_headers,
            )
            self.assertEqual(response.status, 400)

            outside_path.write_bytes(b"must-not-escape")
            output_path.unlink()
            output_path.symlink_to(outside_path)
            response = await client.get(
                f"/__worker/output?{query}", headers=good_headers
            )
            self.assertEqual(response.status, 404)
            output_path.unlink()
            output_path.mkdir()
            response = await client.get(
                f"/__worker/output?{query}", headers=good_headers
            )
            self.assertEqual(response.status, 404)
        finally:
            if output_path.is_dir() and not output_path.is_symlink():
                output_path.rmdir()
            else:
                output_path.unlink(missing_ok=True)
            outside_path.unlink(missing_ok=True)
            await client.close()

    async def test_output_download_is_unavailable_after_reset(self) -> None:
        comfy = FakeComfyClient()
        barrier = FakeBarrier(BarrierResult(success=True, action="ready"))
        client, _server = await self._client(
            capability="worker-cap",
            barrier=barrier,
            comfy_client=comfy,
        )
        headers = {gateway.WORKER_CAPABILITY_HEADER: "worker-cap"}
        claim_identity = {
            "user_id": "user-a",
            "workspace_id": "workspace-a",
            "assignment_id": "assignment-a",
            "job_id": "job-a",
            "affinity_key": "plan-a",
            "assignment_token": "assignment-secret",
        }
        execution_identity = {**claim_identity, "execution_id": "execution-a"}
        output_path = Path("/tmp/comfy-runtime/inst_gatewaytest/output/result.png")
        try:
            await client.post("/__worker/claim", headers=headers, json=claim_identity)
            await client.post(
                "/__worker/execute",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json={
                    **execution_identity,
                    "workflow": {"3": {"class_type": "Test"}},
                },
            )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"reset-me")
            comfy.history_value = {
                "comfy-prompt-1": {
                    "status": {"status_str": "success", "completed": True},
                    "outputs": {
                        "3": {
                            "images": [{"filename": "result.png", "type": "output"}]
                        }
                    },
                }
            }
            response = await client.post(
                "/__worker/result",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json=execution_identity,
            )
            output_id = (await response.json())["execution"]["result"]["outputs"]["3"]["images"][0]["output_id"]
            query = (
                "assignment_id=assignment-a&user_id=user-a&workspace_id=workspace-a"
                "&job_id=job-a&affinity_key=plan-a&execution_id=execution-a"
                f"&output_id={output_id}"
            )
            response = await client.post(
                "/__worker/complete",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json={**claim_identity, "status": "completed"},
            )
            self.assertEqual(response.status, 200)
            response = await client.post(
                "/__worker/reset",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json=claim_identity,
            )
            self.assertEqual(response.status, 200)
            response = await client.get(
                f"/__worker/output?{query}",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
            )
            self.assertNotEqual(response.status, 200)
        finally:
            await client.close()

    async def test_terminal_output_rejects_path_traversal(self) -> None:
        comfy = FakeComfyClient()
        client, _server = await self._client(
            capability="worker-cap",
            comfy_client=comfy,
        )
        headers = {gateway.WORKER_CAPABILITY_HEADER: "worker-cap"}
        claim_identity = {
            "user_id": "user-a",
            "workspace_id": "workspace-a",
            "assignment_id": "assignment-a",
            "job_id": "job-a",
            "affinity_key": "plan-a",
            "assignment_token": "assignment-secret",
        }
        execution_identity = {**claim_identity, "execution_id": "execution-a"}
        try:
            await client.post("/__worker/claim", headers=headers, json=claim_identity)
            await client.post(
                "/__worker/execute",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json={
                    **execution_identity,
                    "workflow": {"3": {"class_type": "Test"}},
                },
            )
            comfy.history_value = {
                "comfy-prompt-1": {
                    "status": {"status_str": "success", "completed": True},
                    "outputs": {
                        "3": {
                            "images": [
                                {"filename": "../escape.png", "type": "output"}
                            ]
                        }
                    },
                }
            }
            response = await client.post(
                "/__worker/result",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json=execution_identity,
            )
            self.assertEqual(response.status, 502)
            self.assertNotIn("escape.png", await response.text())
        finally:
            await client.close()

    async def test_cancel_calls_interrupt_and_queue_removal_once_and_requires_complete(self) -> None:
        comfy = FakeComfyClient()
        client, _server = await self._client(
            capability="worker-cap",
            comfy_client=comfy,
        )
        headers = {gateway.WORKER_CAPABILITY_HEADER: "worker-cap"}
        identity = {
            "user_id": "user-a",
            "workspace_id": "workspace-a",
            "assignment_id": "assignment-a",
            "job_id": "job-a",
            "affinity_key": "plan-a",
            "assignment_token": "assignment-secret",
            "execution_id": "execution-a",
        }
        claim_identity = {
            key: value for key, value in identity.items() if key != "execution_id"
        }
        try:
            response = await client.post(
                "/__worker/claim", headers=headers, json=claim_identity
            )
            self.assertEqual(response.status, 200)
            await client.post(
                "/__worker/execute",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json={**identity, "workflow": {"3": {"class_type": "Test"}}},
            )

            response = await client.post(
                "/__worker/cancel",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json=identity,
            )
            self.assertEqual(response.status, 200)
            cancelled = await response.json()
            self.assertEqual(cancelled["kind"], "cancel_requested")
            self.assertEqual(cancelled["execution"]["status"], "queued")
            self.assertEqual(comfy.interrupt_calls, ["comfy-prompt-1"])
            self.assertEqual(comfy.remove_calls, ["comfy-prompt-1"])

            response = await client.post(
                "/__worker/cancel",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json=identity,
            )
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["kind"], "cancel_requested")
            self.assertEqual(comfy.interrupt_calls, ["comfy-prompt-1"])
            self.assertEqual(comfy.remove_calls, ["comfy-prompt-1"])

            response = await client.post(
                "/__worker/result",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json=identity,
            )
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["kind"], "pending")

            response = await client.post(
                "/__worker/result",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json=identity,
            )
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["kind"], "cancelled")

            response = await client.post(
                "/__worker/complete",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json={**claim_identity, "status": "completed"},
            )
            self.assertEqual(response.status, 409)
            response = await client.post(
                "/__worker/complete",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json={**claim_identity, "status": "cancelled"},
            )
            self.assertEqual(response.status, 200)
        finally:
            await client.close()

    async def test_cancel_partial_failure_is_retryable_and_result_waits_for_queue_confirmation(self) -> None:
        comfy = FakeComfyClient()
        comfy.remove_error = gateway.ComfyExecutionError("provider secret/path")
        client, _server = await self._client(
            capability="worker-cap",
            comfy_client=comfy,
        )
        headers = {gateway.WORKER_CAPABILITY_HEADER: "worker-cap"}
        identity = {
            "user_id": "user-a",
            "workspace_id": "workspace-a",
            "assignment_id": "assignment-a",
            "job_id": "job-a",
            "affinity_key": "plan-a",
            "assignment_token": "assignment-secret",
            "execution_id": "execution-a",
        }
        claim_identity = {
            key: value for key, value in identity.items() if key != "execution_id"
        }
        try:
            response = await client.post(
                "/__worker/claim", headers=headers, json=claim_identity
            )
            self.assertEqual(response.status, 200)
            response = await client.post(
                "/__worker/execute",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json={**identity, "workflow": {"3": {"class_type": "Test"}}},
            )
            self.assertEqual(response.status, 200)

            response = await client.post(
                "/__worker/cancel",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json=identity,
            )
            self.assertEqual(response.status, 502)
            failed = await response.json()
            self.assertEqual(failed["execution"]["status"], "queued")
            self.assertNotIn("provider secret/path", await response.text())
            self.assertEqual(comfy.interrupt_calls, ["comfy-prompt-1"])
            self.assertEqual(comfy.remove_calls, [])

            comfy.remove_error = None
            response = await client.post(
                "/__worker/cancel",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json=identity,
            )
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["kind"], "cancel_requested")
            self.assertEqual(comfy.interrupt_calls, ["comfy-prompt-1"])
            self.assertEqual(comfy.remove_calls, ["comfy-prompt-1"])

            # A queue entry keeps the execution non-terminal even after the
            # interrupt/delete calls have succeeded.
            comfy.queue_value = {
                "queue_running": [],
                "queue_pending": [[7, "comfy-prompt-1", {"private": True}]],
            }
            response = await client.post(
                "/__worker/result",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json=identity,
            )
            self.assertEqual(response.status, 200)
            pending = await response.json()
            self.assertEqual(pending["kind"], "pending")
            self.assertEqual(pending["execution"]["status"], "queued")

            comfy.queue_value = {"queue_running": [], "queue_pending": []}
            response = await client.post(
                "/__worker/result",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json=identity,
            )
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["kind"], "pending")
            response = await client.post(
                "/__worker/result",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json=identity,
            )
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["kind"], "cancelled")
        finally:
            await client.close()

    async def test_unknown_history_status_never_confirms_cancellation(self) -> None:
        comfy = FakeComfyClient()
        client, _server = await self._client(
            capability="worker-cap",
            comfy_client=comfy,
        )
        headers = {gateway.WORKER_CAPABILITY_HEADER: "worker-cap"}
        identity = {
            "user_id": "user-a",
            "workspace_id": "workspace-a",
            "assignment_id": "assignment-a",
            "job_id": "job-a",
            "affinity_key": "plan-a",
            "assignment_token": "assignment-secret",
            "execution_id": "execution-a",
        }
        claim_identity = {
            key: value for key, value in identity.items() if key != "execution_id"
        }
        try:
            response = await client.post(
                "/__worker/claim", headers=headers, json=claim_identity
            )
            self.assertEqual(response.status, 200)
            response = await client.post(
                "/__worker/execute",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json={**identity, "workflow": {"3": {"class_type": "Test"}}},
            )
            self.assertEqual(response.status, 200)
            response = await client.post(
                "/__worker/cancel",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json=identity,
            )
            self.assertEqual(response.status, 200)

            comfy.history_value = {
                "comfy-prompt-1": {
                    "status": {"status_str": "provider_added_status"},
                }
            }
            for _ in range(3):
                response = await client.post(
                    "/__worker/result",
                    headers={
                        **headers,
                        gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                    },
                    json=identity,
                )
                self.assertEqual(response.status, 200)
                pending = await response.json()
                self.assertEqual(pending["kind"], "pending")
                self.assertEqual(pending["execution"]["status"], "queued")

            response = await client.post(
                "/__worker/complete",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json={**claim_identity, "status": "cancelled"},
            )
            self.assertEqual(response.status, 409)
        finally:
            await client.close()

    async def test_terminal_error_is_generic_and_completion_status_is_bound(self) -> None:
        comfy = FakeComfyClient()
        client, _server = await self._client(
            capability="worker-cap",
            comfy_client=comfy,
        )
        headers = {gateway.WORKER_CAPABILITY_HEADER: "worker-cap"}
        identity = {
            "user_id": "user-a",
            "workspace_id": "workspace-a",
            "assignment_id": "assignment-a",
            "job_id": "job-a",
            "affinity_key": "plan-a",
            "assignment_token": "assignment-secret",
            "execution_id": "execution-a",
        }
        claim_identity = {
            key: value for key, value in identity.items() if key != "execution_id"
        }
        try:
            response = await client.post(
                "/__worker/claim", headers=headers, json=claim_identity
            )
            self.assertEqual(response.status, 200)
            await client.post(
                "/__worker/execute",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json={**identity, "workflow": {"3": {"class_type": "Test"}}},
            )
            comfy.history_value = {
                "comfy-prompt-1": {
                    "status": {
                        "status_str": "error",
                        "messages": [
                            [
                                "execution_error",
                                {
                                    "exception_message": "/secret/path/token=do-not-leak",
                                },
                            ]
                        ],
                    }
                }
            }
            response = await client.post(
                "/__worker/result",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json=identity,
            )
            self.assertEqual(response.status, 200)
            result = await response.json()
            self.assertEqual(result["kind"], "failed")
            self.assertEqual(result["execution"]["status"], "failed")
            self.assertEqual(result["execution"]["error_code"], "COMFY_EXECUTION_FAILED")
            self.assertEqual(result["execution"]["error_message"], "ComfyUI execution failed")
            body = await response.text()
            self.assertNotIn("/secret/path", body)
            self.assertNotIn("do-not-leak", body)

            response = await client.post(
                "/__worker/complete",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json={**claim_identity, "status": "completed"},
            )
            self.assertEqual(response.status, 409)
            response = await client.post(
                "/__worker/complete",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json={**claim_identity, "status": "failed"},
            )
            self.assertEqual(response.status, 200)
        finally:
            await client.close()

    async def test_reset_failure_restarts_and_reprobes_before_ready(self) -> None:
        barrier = FakeBarrier(
            BarrierResult(
                success=False,
                action="request_restart",
                reason="baseline mismatch",
            )
        )
        restart_calls: list[str] = []
        with tempfile.TemporaryDirectory() as temp:
            generation_path = Path(temp) / "generation"
            generation_path.write_text("1\n", encoding="ascii")

            def restart() -> bool:
                restart_calls.append("restart")
                generation_path.write_text("2\n", encoding="ascii")
                return True

            client, _server = await self._client(
                capability="worker-cap",
                barrier=barrier,
                restart_callback=restart,
                restart_probe=lambda: {"system": {}},
                restart_generation_path=str(generation_path),
            )
            instance_root = Path("/tmp/comfy-runtime/inst_gatewaytest")
            for name in ("input", "output", "temp", "user"):
                directory = instance_root / name
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "previous-user.txt").write_text(
                    "private",
                    encoding="utf-8",
                )
            headers = {gateway.WORKER_CAPABILITY_HEADER: "worker-cap"}
            identity = {
                "user_id": "user-a",
                "workspace_id": "workspace-a",
            }
            claim_identity = {
                **identity,
                "assignment_id": "assignment-a",
                "assignment_token": "token-assignment-a",
                "job_id": "job-a",
                "affinity_key": "plan-a",
            }
            try:
                response = await client.post(
                    "/__worker/claim",
                    headers=headers,
                    json=claim_identity,
                )
                await response.read()
                assignment_token = "token-assignment-a"
                await client.post(
                    "/__worker/complete",
                    headers={
                        **headers,
                        gateway.ASSIGNMENT_TOKEN_HEADER: assignment_token,
                    },
                    json={**claim_identity, "status": "failed", "keep_warm": False},
                )

                response = await client.post(
                    "/__worker/reset",
                    headers={
                        **headers,
                        gateway.ASSIGNMENT_TOKEN_HEADER: assignment_token,
                    },
                    json=claim_identity,
                )
                self.assertEqual(response.status, 200)
                failed = await response.json()
                self.assertEqual(failed["state"], "ready")
                self.assertTrue(failed["ready"])
                self.assertEqual(failed["kind"], "restarted")
                self.assertEqual(restart_calls, ["restart"])
                for name in ("input", "output", "temp", "user"):
                    directory = instance_root / name
                    self.assertTrue(directory.is_dir())
                    self.assertEqual(list(directory.iterdir()), [])

                response = await client.get("/__worker/status", headers=headers)
                status = await response.json()
                self.assertEqual(status["state"], "ready")
                self.assertTrue(status["ready"])
                self.assertIsNone(status["assignment"])
            finally:
                await client.close()

    async def test_complete_success_requires_a_terminal_execution(self) -> None:
        client, _server = await self._client(capability="worker-cap")
        capability_headers = {gateway.WORKER_CAPABILITY_HEADER: "worker-cap"}
        identity = {
            "user_id": "user-a",
            "workspace_id": "workspace-a",
            "assignment_id": "assignment-a",
            "assignment_token": "assignment-secret",
            "job_id": "job-a",
            "affinity_key": "plan-a",
        }
        try:
            response = await client.post(
                "/__worker/claim",
                headers=capability_headers,
                json=identity,
            )
            self.assertEqual(response.status, 200)

            response = await client.post(
                "/__worker/complete",
                headers={
                    **capability_headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: "assignment-secret",
                },
                json={**identity, "status": "completed", "keep_warm": False},
            )
            self.assertEqual(response.status, 409)
            body = await response.json()
            self.assertFalse(body["ok"])
            self.assertIn("terminal execution", body["error"])
        finally:
            await client.close()

    async def test_restart_failure_stays_error_and_never_ready(self) -> None:
        barrier = FakeBarrier(
            BarrierResult(
                success=False,
                action="request_restart",
                reason="baseline mismatch",
            )
        )
        client, _server = await self._client(
            capability="worker-cap",
            barrier=barrier,
            restart_callback=lambda: False,
        )
        headers = {gateway.WORKER_CAPABILITY_HEADER: "worker-cap"}
        identity = {"user_id": "user-a", "workspace_id": "workspace-a"}
        claim_identity = {
            **identity,
            "assignment_id": "assignment-a",
            "assignment_token": "token-assignment-a",
            "job_id": "job-a",
            "affinity_key": "plan-a",
        }
        try:
            response = await client.post(
                "/__worker/claim",
                headers=headers,
                json=claim_identity,
            )
            await response.read()
            assignment_token = "token-assignment-a"
            await client.post(
                "/__worker/complete",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: assignment_token,
                },
                json={**claim_identity, "status": "failed", "keep_warm": False},
            )
            response = await client.post(
                "/__worker/reset",
                headers={
                    **headers,
                    gateway.ASSIGNMENT_TOKEN_HEADER: assignment_token,
                },
                json=claim_identity,
            )
            self.assertEqual(response.status, 503)
            failed = await response.json()
            self.assertEqual(failed["state"], "error")
            self.assertFalse(failed["ready"])
        finally:
            await client.close()

    async def test_worker_only_mode_never_proxies_with_empty_browser_token(self) -> None:
        app = gateway.create_app(
            token="",
            worker_capability_secret="worker-cap",
            worker_initial_state="ready",
            worker_baseline={"system": {}},
        )
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            response = await client.get(
                "/queue",
                headers={gateway.TOKEN_HEADER: ""},
            )
            self.assertEqual(response.status, 401)
            response = await client.get("/queue")
            self.assertEqual(response.status, 401)
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
