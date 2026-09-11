"""Focused tests for the provider-neutral Pod worker lifecycle prototype."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from typing import Mapping
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "worker_lifecycle", ROOT / "docker" / "pod" / "worker_lifecycle.py"
)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


class FakeTransport:
    def __init__(self, responses: list[module.TransportResponse | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, Mapping[str, object] | None, float]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object] | None = None,
        timeout_seconds: float,
    ) -> module.TransportResponse:
        self.calls.append((method, path, json_body, timeout_seconds))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        self.now += duration


def response(body: object, status_code: int = 200) -> module.TransportResponse:
    return module.TransportResponse(status_code=status_code, body=body)


def stats(*, ram_free: int = 1000, vram_free: int = 2000) -> dict[str, object]:
    return {
        "system": {
            "os": "posix",
            "ram_total": 4000,
            "ram_free": ram_free,
        },
        "devices": [
            {
                "name": "cuda:0 Test GPU",
                "type": "cuda",
                "index": 0,
                "vram_total": 8000,
                "vram_free": vram_free,
            }
        ],
    }


class WorkerStateTests(unittest.TestCase):
    def test_valid_state_machine_clears_affinity_after_reset(self) -> None:
        worker = module.WorkerRecord("worker-1")
        worker = module.transition_worker(worker, module.WorkerEvent.BOOT_SUCCEEDED)
        worker = module.transition_worker(
            worker,
            module.WorkerEvent.JOB_STARTED,
            user_id="user-1",
            workspace_id="workspace-1",
            affinity_key="plan-1",
        )
        worker = module.transition_worker(worker, module.WorkerEvent.JOB_FINISHED)
        self.assertEqual(worker.state, module.WorkerState.USER_WARM)
        self.assertIsNotNone(worker.affinity)
        worker = module.transition_worker(worker, module.WorkerEvent.RESET_REQUESTED)
        self.assertEqual(worker.state, module.WorkerState.CLEANING)
        worker = module.transition_worker(worker, module.WorkerEvent.RESET_SUCCEEDED)
        self.assertEqual(worker.state, module.WorkerState.READY)
        self.assertIsNone(worker.affinity)

    def test_failed_worker_cannot_become_ready_without_restart_boot(self) -> None:
        worker = module.WorkerRecord("worker-1")
        worker = module.transition_worker(worker, module.WorkerEvent.BOOT_FAILED)
        self.assertEqual(worker.state, module.WorkerState.ERROR)
        with self.assertRaises(module.InvalidWorkerTransition):
            module.transition_worker(worker, module.WorkerEvent.RESET_SUCCEEDED)
        worker = module.transition_worker(worker, module.WorkerEvent.RESTART_REQUESTED)
        self.assertEqual(worker.state, module.WorkerState.RESTARTING)
        worker = module.transition_worker(worker, module.WorkerEvent.BOOT_SUCCEEDED)
        self.assertEqual(worker.state, module.WorkerState.READY)

    def test_different_warm_affinity_cannot_start_a_job(self) -> None:
        worker = module.WorkerRecord(
            "worker-1",
            module.WorkerState.USER_WARM,
            module.WorkerAffinity("user-1", "workspace-1", "plan-1"),
        )
        with self.assertRaisesRegex(module.InvalidWorkerTransition, "different"):
            module.transition_worker(
                worker,
                module.WorkerEvent.JOB_STARTED,
                user_id="user-2",
                workspace_id="workspace-2",
                affinity_key="plan-2",
            )

    def test_idle_worker_can_enter_terminating_but_busy_worker_cannot(self) -> None:
        ready = module.WorkerRecord("ready", module.WorkerState.READY)
        terminating = module.transition_worker(
            ready,
            module.WorkerEvent.TERMINATION_REQUESTED,
        )
        self.assertEqual(terminating.state, module.WorkerState.TERMINATING)

        busy = module.WorkerRecord(
            "busy",
            module.WorkerState.BUSY,
            module.WorkerAffinity("user", "workspace", "plan"),
        )
        with self.assertRaises(module.InvalidWorkerTransition):
            module.transition_worker(
                busy,
                module.WorkerEvent.TERMINATION_REQUESTED,
            )


class WarmAffinityTests(unittest.TestCase):
    def test_exact_warm_worker_wins(self) -> None:
        decision = module.choose_warm_worker(
            [
                module.WorkerSnapshot(
                    "busy",
                    module.WorkerState.BUSY,
                    module.WorkerAffinity("u", "w", "plan"),
                ),
                module.WorkerSnapshot(
                    "warm",
                    module.WorkerState.USER_WARM,
                    module.WorkerAffinity("u", "w", "plan"),
                ),
                module.WorkerSnapshot("clean", module.WorkerState.READY),
            ],
            user_id="u",
            workspace_id="w",
            affinity_key="plan",
        )
        self.assertEqual(decision.action, module.WarmAffinityAction.REUSE_WARM)
        self.assertEqual(decision.worker_id, "warm")

    def test_other_user_is_reset_not_reused(self) -> None:
        decision = module.choose_warm_worker(
            [
                module.WorkerSnapshot(
                    "previous-user",
                    module.WorkerState.USER_WARM,
                    module.WorkerAffinity("other", "workspace", "plan-old"),
                )
            ],
            user_id="u",
            workspace_id="w",
            affinity_key="plan",
        )
        self.assertEqual(decision.action, module.WarmAffinityAction.RESET_THEN_ASSIGN)
        self.assertEqual(decision.worker_id, "previous-user")

    def test_busy_workers_do_not_get_reused(self) -> None:
        decision = module.choose_warm_worker(
            [
                module.WorkerSnapshot(
                    "busy",
                    module.WorkerState.BUSY,
                    module.WorkerAffinity("u", "w", "plan"),
                )
            ],
            user_id="u",
            workspace_id="w",
            affinity_key="plan",
        )
        self.assertEqual(decision.action, module.WarmAffinityAction.START_NEW)
        self.assertIsNone(decision.worker_id)

    def test_clean_ready_worker_can_be_used_without_cross_user_state(self) -> None:
        decision = module.choose_warm_worker(
            [module.WorkerSnapshot("clean", module.WorkerState.READY)],
            user_id="u",
            workspace_id="w",
            affinity_key="plan",
        )
        self.assertEqual(decision.action, module.WarmAffinityAction.REUSE_CLEAN)


class BaselineTests(unittest.TestCase):
    def test_memory_drift_within_tolerance_is_allowed(self) -> None:
        check = module.check_system_stats_baseline(
            stats(),
            stats(ram_free=900, vram_free=1900),
        )
        self.assertTrue(check.ok)

    def test_identity_drift_fails_closed(self) -> None:
        changed = stats()
        devices = changed["devices"]
        assert isinstance(devices, list)
        device = devices[0]
        assert isinstance(device, dict)
        device["name"] = "cuda:0 Different GPU"
        check = module.check_system_stats_baseline(stats(), changed)
        self.assertFalse(check.ok)
        self.assertIn("devices.*.name", check.mismatches)

    def test_large_memory_drift_fails_closed(self) -> None:
        check = module.check_system_stats_baseline(
            stats(),
            stats(vram_free=2000 + 512 * 1024 * 1024 + 1),
        )
        self.assertFalse(check.ok)


class ResetBarrierTests(unittest.TestCase):
    def _barrier(
        self,
        transport: FakeTransport,
        *,
        request_restart_on_failure: bool = True,
        clock: Clock | None = None,
    ) -> module.ComfyResetBarrier:
        active_clock = clock or Clock()
        return module.ComfyResetBarrier(
            transport,
            policy=module.CleanupPolicy(
                max_wait_seconds=1.0,
                poll_interval_seconds=0.1,
                request_timeout_seconds=0.5,
                max_attempts=1,
                request_restart_on_failure=request_restart_on_failure,
            ),
            sleep_fn=active_clock.sleep,
            monotonic_fn=active_clock.monotonic,
        )

    def test_success_calls_queue_history_free_and_system_stats(self) -> None:
        transport = FakeTransport(
            [
                response({"queue_running": [], "queue_pending": []}),
                response({}),
                response({"ok": True}),
                response(stats(ram_free=900, vram_free=1900)),
            ]
        )
        result = self._barrier(transport).reset(baseline=stats())
        self.assertTrue(result.success)
        self.assertEqual(result.action, module.CleanupAction.READY)
        self.assertTrue(result.baseline_ok)
        self.assertEqual(
            [(method, path) for method, path, _, _ in transport.calls],
            [
                ("GET", "/queue"),
                ("GET", "/history"),
                ("POST", "/free"),
                ("GET", "/system_stats"),
            ],
        )
        self.assertEqual(transport.calls[2][2], {"unload_models": True, "free_memory": True})

    def test_history_is_cleared_before_free(self) -> None:
        transport = FakeTransport(
            [
                response({"queue_running": [], "queue_pending": []}),
                response({"old-prompt": {"outputs": {}}}),
                response({"cleared": True}),
                response({}),
                response({"ok": True}),
                response(stats()),
            ]
        )
        result = self._barrier(transport).reset(baseline=stats())
        self.assertTrue(result.success)
        self.assertEqual(
            [(method, path) for method, path, _, _ in transport.calls],
            [
                ("GET", "/queue"),
                ("GET", "/history"),
                ("POST", "/history"),
                ("GET", "/history"),
                ("POST", "/free"),
                ("GET", "/system_stats"),
            ],
        )

    def test_nonempty_queue_times_out_and_requests_restart(self) -> None:
        clock = Clock()
        transport = FakeTransport(
            [response({"queue_running": [{"id": "job"}], "queue_pending": []})] * 20
        )
        result = self._barrier(transport, clock=clock).reset(baseline=stats())
        self.assertFalse(result.success)
        self.assertEqual(result.phase, "queue")
        self.assertEqual(result.action, module.CleanupAction.REQUEST_RESTART)
        self.assertFalse(result.queue_empty)
        self.assertGreaterEqual(len(transport.calls), 11)

    def test_baseline_failure_requests_replacement_when_restart_disabled(self) -> None:
        clock = Clock()
        transport = FakeTransport(
            [
                response({"queue_running": [], "queue_pending": []}),
                response({}),
                response({"ok": True}),
                *[
                    response({"system": {"os": "different"}, "devices": []})
                    for _ in range(12)
                ],
            ]
        )
        result = self._barrier(
            transport, request_restart_on_failure=False, clock=clock
        ).reset(baseline=stats())
        self.assertFalse(result.success)
        self.assertEqual(result.phase, "baseline")
        self.assertEqual(result.action, module.CleanupAction.REQUIRE_REPLACEMENT)
        self.assertFalse(result.baseline_ok)

    def test_free_waits_for_memory_to_converge_to_baseline(self) -> None:
        transport = FakeTransport(
            [
                response({"queue_running": [], "queue_pending": []}),
                response({}),
                response({"ok": True}),
                response(stats(vram_free=2_000 + 512 * 1024 * 1024 + 1)),
                response(stats(ram_free=900, vram_free=1900)),
            ]
        )
        result = self._barrier(transport).reset(baseline=stats())
        self.assertTrue(result.success)
        self.assertEqual(
            [path for method, path, _, _ in transport.calls if method == "GET"],
            ["/queue", "/history", "/system_stats", "/system_stats"],
        )

    def test_malformed_queue_response_fails_closed(self) -> None:
        transport = FakeTransport([response({"queue_running": []})])
        result = self._barrier(transport).reset(baseline=stats())
        self.assertFalse(result.success)
        self.assertEqual(result.phase, "queue")
        self.assertEqual(result.action, module.CleanupAction.REQUEST_RESTART)

    def test_retryable_transport_error_is_bounded(self) -> None:
        transport = FakeTransport(
            [
                TimeoutError("timeout"),
                response({"queue_running": [], "queue_pending": []}),
                response({}),
                response({"ok": True}),
                response(stats()),
            ]
        )
        barrier = module.ComfyResetBarrier(
            transport,
            policy=module.CleanupPolicy(max_attempts=2),
            sleep_fn=lambda _duration: None,
        )
        result = barrier.reset(baseline=stats())
        self.assertTrue(result.success)
        self.assertEqual(result.action, module.CleanupAction.READY)
        self.assertEqual(len(transport.calls), 5)


if __name__ == "__main__":
    unittest.main()
