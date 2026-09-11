"""Provider-neutral lifecycle and reset decisions for a ComfyUI worker.

This module deliberately stops at the container boundary.  It does not know
about RunPod (or any other provider), does not perform provider mutations, and
does not hold credentials.  A control plane can use the state machine and the
pure affinity decision before calling :class:`ComfyResetBarrier` over an
abstract transport.

The reset barrier is intentionally conservative: a malformed response, a
non-empty queue/history that does not drain before the deadline, a failed
``/free`` call, or a baseline mismatch can never produce a ``ready`` result.
The caller decides whether the structured failure should trigger a ComfyUI
restart or replacement of the whole worker.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from fnmatch import fnmatchcase
import math
import time
from typing import Protocol


class WorkerLifecycleError(RuntimeError):
    """Base error for invalid local lifecycle operations."""


class InvalidWorkerTransition(WorkerLifecycleError):
    """Raised when an event cannot be applied to the current worker state."""


class TransportRequestError(WorkerLifecycleError):
    """Raised when the ComfyUI transport cannot produce a valid response."""


class InvalidComfyPayload(WorkerLifecycleError):
    """Raised when ComfyUI returns an unexpected response shape."""


class WorkerState(str, Enum):
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"
    USER_WARM = "user_warm"
    CLEANING = "cleaning"
    RESTARTING = "restarting"
    TERMINATING = "terminating"
    ERROR = "error"


class WorkerEvent(str, Enum):
    BOOT_SUCCEEDED = "boot_succeeded"
    BOOT_FAILED = "boot_failed"
    JOB_STARTED = "job_started"
    JOB_FINISHED = "job_finished"
    JOB_FAILED = "job_failed"
    RESET_REQUESTED = "reset_requested"
    RESET_SUCCEEDED = "reset_succeeded"
    RESET_FAILED = "reset_failed"
    WORKER_FAILED = "worker_failed"
    RESTART_REQUESTED = "restart_requested"
    TERMINATION_REQUESTED = "termination_requested"


@dataclass(frozen=True)
class WorkerAffinity:
    """The identity of the mutable state currently warm on a worker.

    User/workspace identity alone is not sufficient for warm reuse: a model
    plan, image, GPU, or shared-volume change can make the retained process
    state unsafe to reuse.  The control plane therefore supplies one opaque
    ``affinity_key`` covering that immutable execution identity.
    """

    user_id: str
    workspace_id: str
    affinity_key: str

    def __post_init__(self) -> None:
        if not self.user_id or not self.workspace_id or not self.affinity_key:
            raise ValueError(
                "worker affinity requires user_id, workspace_id, and affinity_key"
            )
        if (
            len(self.user_id) > 256
            or len(self.workspace_id) > 256
            or len(self.affinity_key) > 256
            or any("\x00" in value for value in (self.user_id, self.workspace_id, self.affinity_key))
        ):
            raise ValueError("worker affinity identity is invalid")

    @property
    def model_plan_fingerprint(self) -> str:
        """Compatibility spelling for callers using the D1 column name."""

        return self.affinity_key


@dataclass(frozen=True)
class WorkerRecord:
    """Immutable local state used by the lifecycle transition function."""

    worker_id: str
    state: WorkerState = WorkerState.STARTING
    affinity: WorkerAffinity | None = None

    def __post_init__(self) -> None:
        if not self.worker_id:
            raise ValueError("worker_id is required")
        if self.state in {WorkerState.BUSY, WorkerState.USER_WARM}:
            if self.affinity is None:
                raise ValueError(f"{self.state.value} worker requires affinity")
        elif self.state is WorkerState.READY and self.affinity is not None:
            raise ValueError("ready worker must be clean and have no affinity")


def _require_identity(
    user_id: str | None,
    workspace_id: str | None,
    affinity_key: str | None,
) -> WorkerAffinity:
    if not user_id or not workspace_id or not affinity_key:
        raise InvalidWorkerTransition(
            "job_started requires user_id, workspace_id, and affinity_key"
        )
    try:
        return WorkerAffinity(
            user_id=user_id,
            workspace_id=workspace_id,
            affinity_key=affinity_key,
        )
    except ValueError as error:
        raise InvalidWorkerTransition(str(error)) from error


def transition_worker(
    worker: WorkerRecord,
    event: WorkerEvent,
    *,
    user_id: str | None = None,
    workspace_id: str | None = None,
    affinity_key: str | None = None,
) -> WorkerRecord:
    """Apply one explicit lifecycle event and return a new worker record.

    In particular, there is no transition from ``error`` or ``terminating``
    directly to ``ready``.  An errored worker must be restarted and booted
    again, or replaced.  Reset success also clears the warm affinity so a
    future user can only be admitted after a verified clean barrier.
    """

    state = worker.state

    if event is WorkerEvent.BOOT_SUCCEEDED and state in {
        WorkerState.STARTING,
        WorkerState.RESTARTING,
    }:
        return replace(worker, state=WorkerState.READY, affinity=None)
    if event is WorkerEvent.BOOT_FAILED and state in {
        WorkerState.STARTING,
        WorkerState.RESTARTING,
    }:
        return replace(worker, state=WorkerState.ERROR, affinity=None)

    if event is WorkerEvent.JOB_STARTED and state in {
        WorkerState.READY,
        WorkerState.USER_WARM,
    }:
        requested = _require_identity(user_id, workspace_id, affinity_key)
        if worker.affinity is not None and worker.affinity != requested:
            raise InvalidWorkerTransition(
                "worker has a different warm affinity; reset it before reuse"
            )
        return replace(worker, state=WorkerState.BUSY, affinity=requested)

    if event is WorkerEvent.JOB_FINISHED and state is WorkerState.BUSY:
        return replace(worker, state=WorkerState.USER_WARM)
    if event is WorkerEvent.JOB_FAILED and state is WorkerState.BUSY:
        return replace(worker, state=WorkerState.CLEANING)

    if event is WorkerEvent.RESET_REQUESTED and state in {
        WorkerState.READY,
        WorkerState.USER_WARM,
        WorkerState.CLEANING,
    }:
        return replace(worker, state=WorkerState.CLEANING)
    if event is WorkerEvent.RESET_SUCCEEDED and state is WorkerState.CLEANING:
        return replace(worker, state=WorkerState.READY, affinity=None)
    if event is WorkerEvent.RESET_FAILED and state is WorkerState.CLEANING:
        return replace(worker, state=WorkerState.ERROR)

    if event is WorkerEvent.WORKER_FAILED and state not in {
        WorkerState.ERROR,
        WorkerState.TERMINATING,
    }:
        return replace(worker, state=WorkerState.ERROR)
    if event is WorkerEvent.RESTART_REQUESTED and state is WorkerState.ERROR:
        return replace(worker, state=WorkerState.RESTARTING, affinity=None)
    if event is WorkerEvent.TERMINATION_REQUESTED and state in {
        WorkerState.STARTING,
        WorkerState.READY,
        WorkerState.USER_WARM,
        WorkerState.ERROR,
        WorkerState.CLEANING,
        WorkerState.RESTARTING,
    }:
        return replace(worker, state=WorkerState.TERMINATING, affinity=None)

    raise InvalidWorkerTransition(
        f"cannot apply {event.value} to worker {worker.worker_id} in {state.value}"
    )


@dataclass(frozen=True)
class WorkerSnapshot:
    """Read-only worker data accepted by the pure affinity selector."""

    worker_id: str
    state: WorkerState
    affinity: WorkerAffinity | None = None


class WarmAffinityAction(str, Enum):
    REUSE_WARM = "reuse_warm"
    REUSE_CLEAN = "reuse_clean"
    RESET_THEN_ASSIGN = "reset_then_assign"
    START_NEW = "start_new"


@dataclass(frozen=True)
class WarmAffinityDecision:
    action: WarmAffinityAction
    worker_id: str | None
    reason: str


def choose_warm_worker(
    workers: Sequence[WorkerSnapshot],
    *,
    user_id: str,
    workspace_id: str,
    affinity_key: str,
) -> WarmAffinityDecision:
    """Choose a worker without touching a provider or mutable external state.

    Exact user/workspace affinity wins.  A clean ready worker is the next
    choice.  A ready worker belonging to another user/workspace is returned
    only as ``reset_then_assign``; it is never returned as directly reusable.
    Busy and transitional workers are not directly reusable.
    """

    try:
        requested = WorkerAffinity(
            user_id=user_id,
            workspace_id=workspace_id,
            affinity_key=affinity_key,
        )
    except ValueError as error:
        raise InvalidWorkerTransition(str(error)) from error
    ids = [worker.worker_id for worker in workers]
    if len(ids) != len(set(ids)):
        return WarmAffinityDecision(
            WarmAffinityAction.START_NEW,
            None,
            "duplicate worker ids make affinity selection ambiguous",
        )

    warm = [worker for worker in workers if worker.state is WorkerState.USER_WARM]
    exact = sorted(
        (worker for worker in warm if worker.affinity == requested),
        key=lambda worker: worker.worker_id,
    )
    if exact:
        return WarmAffinityDecision(
            WarmAffinityAction.REUSE_WARM,
            exact[0].worker_id,
            "ready worker has exact user/workspace warm affinity",
        )

    clean = sorted(
        (worker for worker in workers if worker.state is WorkerState.READY),
        key=lambda worker: worker.worker_id,
    )
    if clean:
        return WarmAffinityDecision(
            WarmAffinityAction.REUSE_CLEAN,
            clean[0].worker_id,
            "ready worker has no previous user/workspace affinity",
        )

    resettable = sorted(warm, key=lambda worker: worker.worker_id)
    if resettable:
        return WarmAffinityDecision(
            WarmAffinityAction.RESET_THEN_ASSIGN,
            resettable[0].worker_id,
            "worker belongs to another user/workspace and needs a verified reset",
        )

    return WarmAffinityDecision(
        WarmAffinityAction.START_NEW,
        None,
        "no ready worker is available; busy or transitional workers cannot be reused",
    )


@dataclass(frozen=True)
class TransportResponse:
    status_code: int
    body: object = None


class ComfyTransport(Protocol):
    """Minimal HTTP boundary needed by the reset barrier."""

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object] | None = None,
        timeout_seconds: float,
    ) -> TransportResponse:
        ...


@dataclass(frozen=True)
class StatsTolerance:
    """Explicit allow-list for expected runtime counter drift.

    Unknown numeric paths are exact-match by default.  The defaults cover
    memory counters that naturally vary while a process is idle; identity,
    version, device, and topology fields remain exact.
    """

    numeric_deltas: Mapping[str, float] = field(
        default_factory=lambda: {
            "system.ram_free": 256 * 1024 * 1024,
            "system.ram_used": 256 * 1024 * 1024,
            "devices.*.vram_free": 512 * 1024 * 1024,
            "devices.*.vram_used": 512 * 1024 * 1024,
            "devices.*.torch_vram_free": 512 * 1024 * 1024,
            "devices.*.torch_vram_used": 512 * 1024 * 1024,
        }
    )

    def __post_init__(self) -> None:
        for path, limit in self.numeric_deltas.items():
            if not path or not math.isfinite(limit) or limit < 0:
                raise ValueError(f"invalid stats tolerance for {path!r}")

    def limit_for(self, path: str) -> float | None:
        matches = [
            (pattern, limit)
            for pattern, limit in self.numeric_deltas.items()
            if fnmatchcase(path, pattern)
        ]
        if not matches:
            return None
        return max(matches, key=lambda item: len(item[0]))[1]


@dataclass(frozen=True)
class BaselineCheck:
    ok: bool
    mismatches: tuple[str, ...] = ()

    @property
    def reason(self) -> str:
        if self.ok:
            return "system stats are within the configured baseline tolerance"
        return "system stats baseline mismatch: " + ", ".join(self.mismatches)


def check_system_stats_baseline(
    expected: Mapping[str, object],
    actual: Mapping[str, object],
    *,
    tolerance: StatsTolerance | None = None,
) -> BaselineCheck:
    """Compare two ``/system_stats`` payloads with an explicit tolerance."""

    selected = tolerance or StatsTolerance()
    mismatches: list[str] = []

    def compare(left: object, right: object, path: str) -> None:
        if isinstance(left, Mapping):
            if not isinstance(right, Mapping):
                mismatches.append(path or "<root>")
                return
            left_keys = set(left)
            right_keys = set(right)
            for key in sorted(left_keys - right_keys):
                mismatches.append(f"{path}.{key}" if path else str(key))
            for key in sorted(right_keys - left_keys):
                mismatches.append(f"{path}.{key}" if path else str(key))
            for key in sorted(left_keys & right_keys):
                child = f"{path}.{key}" if path else str(key)
                compare(left[key], right[key], child)
            return

        if isinstance(left, Sequence) and not isinstance(left, (str, bytes, bytearray)):
            if not isinstance(right, Sequence) or isinstance(
                right, (str, bytes, bytearray)
            ):
                mismatches.append(path or "<root>")
                return
            if len(left) != len(right):
                mismatches.append(f"{path}.length" if path else "<root>.length")
                return
            for left_item, right_item in zip(left, right):
                child = f"{path}.*" if path else "*"
                compare(left_item, right_item, child)
            return

        left_is_number = isinstance(left, (int, float)) and not isinstance(left, bool)
        right_is_number = isinstance(right, (int, float)) and not isinstance(
            right, bool
        )
        if left_is_number or right_is_number:
            if not left_is_number or not right_is_number:
                mismatches.append(path or "<root>")
                return
            left_number = float(left)
            right_number = float(right)
            if not math.isfinite(left_number) or not math.isfinite(right_number):
                mismatches.append(path or "<root>")
                return
            limit = selected.limit_for(path)
            if limit is None and left != right:
                mismatches.append(path or "<root>")
            elif limit is not None and abs(left_number - right_number) > limit:
                mismatches.append(path or "<root>")
            return

        if left != right:
            mismatches.append(path or "<root>")

    compare(expected, actual, "")
    return BaselineCheck(ok=not mismatches, mismatches=tuple(mismatches))


@dataclass(frozen=True)
class CleanupPolicy:
    max_wait_seconds: float = 20.0
    poll_interval_seconds: float = 0.25
    request_timeout_seconds: float = 3.0
    max_attempts: int = 3
    # A reset spans queue drain, history cleanup, model freeing, and baseline
    # verification.  This is a total budget, not an additional per-phase
    # allowance; callers can therefore reserve time for restart recovery.
    total_timeout_seconds: float = 50.0
    stats_tolerance: StatsTolerance = field(default_factory=StatsTolerance)
    request_restart_on_failure: bool = True

    def __post_init__(self) -> None:
        if self.max_wait_seconds <= 0:
            raise ValueError("max_wait_seconds must be positive")
        if self.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if self.total_timeout_seconds <= 0:
            raise ValueError("total_timeout_seconds must be positive")


class CleanupAction(str, Enum):
    READY = "ready"
    REQUEST_RESTART = "request_restart"
    REQUIRE_REPLACEMENT = "require_replacement"


@dataclass(frozen=True)
class CleanupResult:
    success: bool
    action: CleanupAction
    phase: str
    reason: str
    poll_observations: int
    queue_empty: bool = False
    history_empty: bool = False
    baseline_ok: bool = False
    baseline_mismatches: tuple[str, ...] = ()


def _mapping(payload: object, *, endpoint: str) -> Mapping[str, object]:
    if not isinstance(payload, Mapping):
        raise InvalidComfyPayload(f"{endpoint} returned a non-object payload")
    return payload


def _sequence(value: object, *, field_name: str, endpoint: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise InvalidComfyPayload(f"{endpoint}.{field_name} is not an array")
    return value


def queue_is_empty(payload: object) -> bool:
    """Return whether a ComfyUI ``/queue`` response has no active work."""

    body = _mapping(payload, endpoint="/queue")
    running = _sequence(body.get("queue_running"), field_name="queue_running", endpoint="/queue")
    pending = _sequence(body.get("queue_pending"), field_name="queue_pending", endpoint="/queue")
    return not running and not pending


def history_is_empty(payload: object) -> bool:
    """Return whether a ComfyUI ``/history`` response has no retained entries."""

    body = _mapping(payload, endpoint="/history")
    if set(body) == {"history"}:
        body = _mapping(body["history"], endpoint="/history.history")
    return not body


class ComfyResetBarrier:
    """Bounded, provider-neutral ComfyUI cleanup barrier."""

    _RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})

    def __init__(
        self,
        transport: ComfyTransport,
        *,
        policy: CleanupPolicy | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self._policy = policy or CleanupPolicy()
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, object] | None = None,
        deadline: float | None = None,
    ) -> object:
        last_error = "unknown transport error"
        for attempt in range(1, self._policy.max_attempts + 1):
            remaining = (
                None
                if deadline is None
                else deadline - self._monotonic()
            )
            if remaining is not None and remaining <= 0:
                break
            try:
                response = self._transport.request(
                    method,
                    path,
                    json_body=payload,
                    timeout_seconds=(
                        self._policy.request_timeout_seconds
                        if remaining is None
                        else min(self._policy.request_timeout_seconds, remaining)
                    ),
                )
            except Exception as error:  # transport implementations vary
                last_error = f"transport exception: {error}"
            else:
                if 200 <= response.status_code < 300:
                    return response.body
                last_error = f"HTTP {response.status_code}"
                if response.status_code not in self._RETRYABLE_STATUS_CODES:
                    break
            if attempt < self._policy.max_attempts:
                remaining = (
                    None
                    if deadline is None
                    else deadline - self._monotonic()
                )
                if remaining is not None and remaining <= 0:
                    break
                self._sleep(
                    self._policy.poll_interval_seconds
                    if remaining is None
                    else min(self._policy.poll_interval_seconds, remaining)
                )
        raise TransportRequestError(f"{method} {path} failed: {last_error}")

    def _failure(
        self,
        *,
        phase: str,
        reason: str,
        poll_observations: int,
        queue_empty: bool = False,
        history_empty: bool = False,
        baseline_ok: bool = False,
        baseline_mismatches: tuple[str, ...] = (),
    ) -> CleanupResult:
        action = (
            CleanupAction.REQUEST_RESTART
            if self._policy.request_restart_on_failure
            else CleanupAction.REQUIRE_REPLACEMENT
        )
        return CleanupResult(
            success=False,
            action=action,
            phase=phase,
            reason=reason,
            poll_observations=poll_observations,
            queue_empty=queue_empty,
            history_empty=history_empty,
            baseline_ok=baseline_ok,
            baseline_mismatches=baseline_mismatches,
        )

    def _poll_empty(
        self,
        *,
        endpoint: str,
        predicate: Callable[[object], bool],
        attempts: int,
        deadline: float | None = None,
    ) -> tuple[bool, int, str]:
        phase_deadline = self._monotonic() + self._policy.max_wait_seconds
        if deadline is not None:
            phase_deadline = min(phase_deadline, deadline)
        while True:
            attempts += 1
            try:
                payload = self._request("GET", endpoint, deadline=deadline)
                if predicate(payload):
                    return True, attempts, ""
            except (InvalidComfyPayload, TransportRequestError) as error:
                return False, attempts, str(error)

            remaining = phase_deadline - self._monotonic()
            if remaining <= 0:
                return False, attempts, f"{endpoint} did not become empty before timeout"
            self._sleep(min(self._policy.poll_interval_seconds, remaining))

    def _poll_baseline(
        self,
        *,
        baseline: Mapping[str, object],
        observations: int,
        deadline: float | None = None,
    ) -> tuple[BaselineCheck, int, str]:
        phase_deadline = self._monotonic() + self._policy.max_wait_seconds
        if deadline is not None:
            phase_deadline = min(phase_deadline, deadline)
        last_check = BaselineCheck(ok=False, mismatches=("<not-observed>",))
        while True:
            observations += 1
            try:
                actual = _mapping(
                    self._request(
                        "GET",
                        "/system_stats",
                        deadline=deadline,
                    ),
                    endpoint="/system_stats",
                )
                last_check = check_system_stats_baseline(
                    baseline,
                    actual,
                    tolerance=self._policy.stats_tolerance,
                )
                if last_check.ok:
                    return last_check, observations, ""
            except (InvalidComfyPayload, TransportRequestError) as error:
                return last_check, observations, str(error)

            remaining = phase_deadline - self._monotonic()
            if remaining <= 0:
                return last_check, observations, last_check.reason
            self._sleep(min(self._policy.poll_interval_seconds, remaining))

    def reset(
        self,
        *,
        baseline: Mapping[str, object],
        deadline: float | None = None,
    ) -> CleanupResult:
        """Drain, clear, free, and verify a worker without executing a restart.

        ``baseline`` must be captured when the worker becomes clean and ready.
        Treating a post-job snapshot as its own baseline would let retained
        memory masquerade as a successful reset, so omission is not supported.
        """

        attempts = 0
        reset_deadline = (
            self._monotonic() + self._policy.total_timeout_seconds
            if deadline is None
            else min(deadline, self._monotonic() + self._policy.total_timeout_seconds)
        )
        baseline_payload = baseline

        queue_empty, attempts, reason = self._poll_empty(
            endpoint="/queue",
            predicate=queue_is_empty,
            attempts=attempts,
            deadline=reset_deadline,
        )
        if not queue_empty:
            return self._failure(
                phase="queue",
                reason=reason,
                poll_observations=attempts,
            )

        try:
            first_history = self._request(
                "GET",
                "/history",
                deadline=reset_deadline,
            )
            history_empty = history_is_empty(first_history)
            if not history_empty:
                self._request(
                    "POST",
                    "/history",
                    payload={"clear": True},
                    deadline=reset_deadline,
                )
        except (InvalidComfyPayload, TransportRequestError) as error:
            return self._failure(
                phase="history",
                reason=str(error),
                poll_observations=attempts,
                queue_empty=True,
            )

        if not history_empty:
            history_empty, attempts, reason = self._poll_empty(
                endpoint="/history",
                predicate=history_is_empty,
                attempts=attempts,
                deadline=reset_deadline,
            )
            if not history_empty:
                return self._failure(
                    phase="history",
                    reason=reason,
                    poll_observations=attempts,
                    queue_empty=True,
                )

        try:
            self._request(
                "POST",
                "/free",
                payload={"unload_models": True, "free_memory": True},
                deadline=reset_deadline,
            )
        except (InvalidComfyPayload, TransportRequestError) as error:
            return self._failure(
                phase="free_or_stats",
                reason=str(error),
                poll_observations=attempts,
                queue_empty=True,
                history_empty=True,
            )

        check, attempts, reason = self._poll_baseline(
            baseline=baseline_payload,
            observations=attempts,
            deadline=reset_deadline,
        )
        if not check.ok:
            return self._failure(
                phase="baseline",
                reason=reason,
                poll_observations=attempts,
                queue_empty=True,
                history_empty=True,
                baseline_mismatches=check.mismatches,
            )

        return CleanupResult(
            success=True,
            action=CleanupAction.READY,
            phase="complete",
            reason="queue/history drained, models freed, and baseline verified",
            poll_observations=attempts,
            queue_empty=True,
            history_empty=True,
            baseline_ok=True,
        )
