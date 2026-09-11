"""Authenticated streaming gateway for a RunPod Pod-hosted ComfyUI process.

ComfyUI listens only on loopback.  RunPod exposes this gateway's port through
its HTTPS proxy; the Cloudflare Worker injects ``X-Comfy-Pod-Token`` after it
has authenticated and resolved an editor session.  The browser never receives
the Pod credential or provider URL.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
import copy
import hashlib
import importlib
import importlib.util
import inspect
import hmac
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import sys
import time
from typing import BinaryIO, Protocol
from urllib.parse import quote
import urllib.error
import urllib.request

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web


TOKEN_HEADER = "X-Comfy-Pod-Token"
HEALTH_PATH = "/__comfy/health"
WORKER_PREFIX = "/__worker"
WORKER_CAPABILITY_ENV = "COMFY_POD_WORKER_CAPABILITY_SECRET"
WORKER_CAPABILITY_HEADER = "X-Comfy-Worker-Capability"
ASSIGNMENT_TOKEN_HEADER = "X-Comfy-Worker-Assignment"
EXECUTION_PATH = f"{WORKER_PREFIX}/execute"
RESULT_PATH = f"{WORKER_PREFIX}/result"
CANCEL_PATH = f"{WORKER_PREFIX}/cancel"
OUTPUT_PATH = f"{WORKER_PREFIX}/output"
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# The worker protocol is an internal control-plane boundary, but it is still
# reachable through a provider HTTP proxy.  Keep both request and response
# budgets explicit so a malformed or compromised control-plane caller cannot
# turn the Pod into an unbounded JSON relay.
MAX_WORKER_REQUEST_BYTES = 8 * 1024 * 1024
MAX_WORKFLOW_BYTES = 4 * 1024 * 1024
MAX_WORKER_RESPONSE_BYTES = 128 * 1024
MAX_WORKFLOW_DEPTH = 64
MAX_WORKFLOW_COLLECTION_ITEMS = 50_000
MAX_OUTPUT_NODES = 256
MAX_OUTPUT_FILES = 64
MAX_OUTPUT_FIELD_LENGTH = 512
# Output bytes are streamed from the instance-local filesystem, but terminal
# manifests still need an explicit disk/transfer budget.  These limits keep a
# malicious or accidentally enormous Comfy result from turning one worker
# execution into an unbounded control-plane resource.
MAX_OUTPUT_FILE_BYTES = 256 * 1024 * 1024
MAX_OUTPUT_TOTAL_BYTES = 1 * 1024 * 1024 * 1024
OUTPUT_STREAM_CHUNK_BYTES = 64 * 1024
TERMINAL_EXECUTION_STATES = frozenset({"completed", "failed", "cancelled"})
PUBLIC_EXECUTION_STATES = frozenset({"queued", "running", *TERMINAL_EXECUTION_STATES})
# `/history/<prompt_id>` may echo the submitted workflow (up to 4 MiB) before
# the controller filters it down to the 128 KiB wire manifest. Keep this
# upstream read bounded without making ordinary large workflows impossible.
MAX_COMFY_RESPONSE_BYTES = 8 * 1024 * 1024
MIN_CANCEL_QUEUE_CONFIRMATIONS = 2
RESET_TOTAL_TIMEOUT_SECONDS = 50.0


def _load_worker_lifecycle() -> object | None:
    """Load the optional provider-neutral lifecycle module.

    ``gateway.py`` is copied into the Pod image independently of the source
    tree.  Keeping this import optional makes a missing protocol asset fail
    closed (the normal ComfyUI proxy still works) instead of preventing the
    image from starting.  Source-tree tests and development images discover a
    sibling ``worker_lifecycle.py`` without requiring a package marker.
    """

    existing = sys.modules.get("worker_lifecycle")
    if existing is not None:
        return existing
    try:
        return importlib.import_module("worker_lifecycle")
    except ModuleNotFoundError as error:
        if error.name != "worker_lifecycle":
            raise
    sibling = Path(__file__).with_name("worker_lifecycle.py")
    if not sibling.is_file():
        return None
    spec = importlib.util.spec_from_file_location("worker_lifecycle", sibling)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules["worker_lifecycle"] = module
    spec.loader.exec_module(module)
    return module


_WORKER_LIFECYCLE = _load_worker_lifecycle()


class WorkerProtocolError(ValueError):
    """A malformed or unauthorized request to the internal worker protocol."""


class WorkerRequestTooLarge(WorkerProtocolError):
    """The bounded worker JSON request exceeded its byte budget."""


class WorkerProtocolUnavailable(RuntimeError):
    """The opt-in protocol cannot run because its lifecycle asset is absent."""


class RestartCallback(Protocol):
    def __call__(self) -> bool | None | Awaitable[bool | None]:
        ...


def configured_worker_capability_secret() -> str | None:
    """Return the dedicated worker capability, or ``None`` when disabled.

    The first name is the canonical deployment setting.  The aliases are
    intentionally narrow compatibility spellings for early Pod images; all
    still represent a separate capability and never fall back to the browser
    ``COMFY_POD_TOKEN``.
    """

    for name in (
        WORKER_CAPABILITY_ENV,
        "COMFY_WORKER_CAPABILITY_SECRET",
        "COMFY_POD_CAPABILITY_SECRET",
    ):
        value = os.environ.get(name, "")
        if value:
            return value
    return None


def capability_matches(provided: str | None, expected: str) -> bool:
    return provided is not None and hmac.compare_digest(provided, expected)


def _header_value(request: web.Request, *names: str) -> str | None:
    for name in names:
        value = request.headers.get(name)
        if value is not None:
            return value
    return None


def configured_token() -> str:
    token = os.environ.get("COMFY_POD_TOKEN", "")
    if not token:
        raise RuntimeError("COMFY_POD_TOKEN is required")
    return token


def token_matches(provided: str | None, expected: str) -> bool:
    return provided is not None and hmac.compare_digest(provided, expected)


def upstream_base_url() -> str:
    host = os.environ.get("COMFY_INTERNAL_HOST", "127.0.0.1")
    port = int(os.environ.get("COMFY_INTERNAL_PORT", "8188"))
    return f"http://{host}:{port}"


def forwarded_headers(request: web.Request) -> dict[str, str]:
    blocked = HOP_BY_HOP_HEADERS | {
        "host",
        "content-length",
        TOKEN_HEADER.lower(),
        WORKER_CAPABILITY_HEADER.lower(),
        "x-comfy-pod-worker-capability",
        "x-comfy-pod-capability",
        ASSIGNMENT_TOKEN_HEADER.lower(),
        "x-comfy-assignment-token",
    }
    return {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in blocked
    }


def response_headers(headers: object) -> dict[str, str]:
    items = getattr(headers, "items")()
    return {
        name: value
        for name, value in items
        if name.lower() not in HOP_BY_HOP_HEADERS | {"content-length"}
    }


class WorkerAssignment:
    """The one in-memory assignment owned by this ComfyUI process."""

    def __init__(
        self,
        assignment_id: str,
        assignment_token: str,
        user_id: str,
        workspace_id: str,
        job_id: str,
        affinity_key: str,
        created_at: int,
        heartbeat_at: int,
    ) -> None:
        self.assignment_id = assignment_id
        self.assignment_token = assignment_token
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.job_id = job_id
        self.affinity_key = affinity_key
        self.created_at = created_at
        self.heartbeat_at = heartbeat_at
        self.status = "claimed"
        self.completion_status: str | None = None
        self.warm_until: int | None = None


class ComfyHTTPTransport:
    """Small synchronous transport used by ``ComfyResetBarrier`` in a thread."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object] | None = None,
        timeout_seconds: float,
        max_response_bytes: int = MAX_COMFY_RESPONSE_BYTES,
    ) -> object:
        if (
            not isinstance(max_response_bytes, int)
            or isinstance(max_response_bytes, bool)
            or max_response_bytes <= 0
        ):
            raise ValueError("ComfyUI response size limit must be positive")
        payload = (
            json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            if json_body is not None
            else None
        )
        request = urllib.request.Request(
            self._base_url + path,
            data=payload,
            headers={"Content-Type": "application/json"} if payload else {},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body = self._read_bounded_body(response, max_response_bytes)
                return self._response(response.status, body)
        except urllib.error.HTTPError as error:
            try:
                body = self._read_bounded_body(error, max_response_bytes)
            finally:
                error.close()
            return self._response(error.code, body)
        except (OSError, TimeoutError) as error:
            raise RuntimeError("ComfyUI request failed") from error

    @staticmethod
    def _read_bounded_body(stream: object, max_response_bytes: int) -> bytes:
        headers = getattr(stream, "headers", None)
        content_length = headers.get("Content-Length") if headers is not None else None
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except (TypeError, ValueError) as error:
                raise RuntimeError("ComfyUI response size is invalid") from error
            if declared_length < 0:
                raise RuntimeError("ComfyUI response size is invalid")
            if declared_length > max_response_bytes:
                raise RuntimeError("ComfyUI response is too large")

        chunks: list[bytes] = []
        total = 0
        read = getattr(stream, "read", None)
        if not callable(read):
            raise RuntimeError("ComfyUI response could not be read")
        while True:
            chunk = read(min(64 * 1024, max_response_bytes - total + 1))
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise RuntimeError("ComfyUI response could not be read")
            chunks.append(chunk)
            total += len(chunk)
            if total > max_response_bytes:
                raise RuntimeError("ComfyUI response is too large")
        return b"".join(chunks)

    @staticmethod
    def _response(status_code: int, body: bytes) -> object:
        if not body:
            parsed: object = None
        else:
            try:
                parsed = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = body.decode("utf-8", errors="replace")
        lifecycle = _WORKER_LIFECYCLE
        if lifecycle is None:
            raise WorkerProtocolUnavailable("worker lifecycle module is unavailable")
        return lifecycle.TransportResponse(status_code=status_code, body=parsed)


class ComfyExecutionError(RuntimeError):
    """ComfyUI rejected or could not complete a worker execution request."""


class ComfyRequestRejected(ComfyExecutionError):
    """ComfyUI returned a definitive non-success response."""


class ComfyRequestUnknown(ComfyExecutionError):
    """The transport outcome is unknown and must not be retried blindly."""


class ComfySubmissionRejected(ComfyExecutionError):
    """ComfyUI definitively rejected a workflow submission."""


class ComfySubmissionUnknown(ComfyExecutionError):
    """A workflow submission may have reached ComfyUI, but its result is unknown."""


class ComfyExecutionClient(Protocol):
    """Small injectable boundary for assignment-bound workflow execution."""

    async def submit(
        self,
        workflow: Mapping[str, object],
        *,
        client_id: str,
    ) -> str:
        ...

    async def history(self, prompt_id: str) -> object:
        ...

    async def queue(self) -> object:
        ...

    async def interrupt(self, prompt_id: str) -> None:
        ...

    async def remove_from_queue(self, prompt_id: str) -> None:
        ...


class ComfyHTTPExecutionClient:
    """Provider-neutral async adapter for ComfyUI's local HTTP API.

    The adapter deliberately returns only the provider prompt id to the
    controller.  The controller never forwards ComfyUI's raw response to the
    control plane, which keeps arbitrary metadata and exception details out of
    the worker protocol.
    """

    def __init__(self, base_url: str | None = None) -> None:
        self._transport = ComfyHTTPTransport(base_url or upstream_base_url())

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object] | None = None,
        allow_status: frozenset[int] = frozenset(),
        timeout_seconds: float,
    ) -> object:
        try:
            response = await asyncio.to_thread(
                self._transport.request,
                method,
                path,
                json_body=json_body,
                timeout_seconds=timeout_seconds,
                max_response_bytes=MAX_COMFY_RESPONSE_BYTES,
            )
        except Exception as error:
            raise ComfyRequestUnknown("ComfyUI request outcome is unknown") from error
        status_code = getattr(response, "status_code", 0)
        if status_code not in range(200, 300) and status_code not in allow_status:
            raise ComfyRequestRejected("ComfyUI request was rejected")
        if status_code in allow_status and status_code not in range(200, 300):
            return None
        return getattr(response, "body", None)

    async def submit(
        self,
        workflow: Mapping[str, object],
        *,
        client_id: str,
    ) -> str:
        try:
            body = await self._request(
                "POST",
                "/prompt",
                json_body={"prompt": workflow, "client_id": client_id},
                timeout_seconds=15.0,
            )
        except ComfyRequestRejected as error:
            raise ComfySubmissionRejected("ComfyUI rejected the workflow") from error
        except ComfyRequestUnknown as error:
            raise ComfySubmissionUnknown("ComfyUI submission outcome is unknown") from error
        if not isinstance(body, Mapping):
            raise ComfySubmissionRejected("ComfyUI returned an invalid prompt response")
        prompt_id = body.get("prompt_id")
        try:
            return _valid_identifier(prompt_id, "prompt id")
        except WorkerProtocolError as error:
            raise ComfySubmissionRejected("ComfyUI returned an invalid prompt id") from error

    async def history(self, prompt_id: str) -> object:
        # ComfyUI prompt ids are normally UUIDs, but quote the complete value
        # so a future provider change cannot turn an id into a path segment.
        value = await self._request(
            "GET",
            f"/history/{quote(prompt_id, safe='')}",
            allow_status=frozenset({404}),
            timeout_seconds=10.0,
        )
        return {} if value is None else value

    async def queue(self) -> object:
        value = await self._request("GET", "/queue", timeout_seconds=10.0)
        return {} if value is None else value

    async def interrupt(self, prompt_id: str) -> None:
        # A missing prompt is already interrupted.  Treat provider 404 as an
        # idempotent success, while all other failures remain fail-closed.
        await self._request(
            "POST",
            "/interrupt",
            json_body={"prompt_id": prompt_id},
            allow_status=frozenset({404}),
            timeout_seconds=5.0,
        )

    async def remove_from_queue(self, prompt_id: str) -> None:
        # Queue removal is also idempotent: an already running/removed prompt
        # may legitimately be absent from the pending queue.
        await self._request(
            "POST",
            "/queue",
            json_body={"delete": [prompt_id]},
            allow_status=frozenset({404}),
            timeout_seconds=5.0,
        )


class WorkerOutput:
    """A verified, execution-scoped output and its public metadata."""

    def __init__(
        self,
        *,
        output_id: str,
        path: Path,
        filename: str,
        subfolder: str,
        category: str,
        size_bytes: int,
        sha256: str,
        content_type: str,
    ) -> None:
        self.output_id = output_id
        self.path = path
        self.filename = filename
        self.subfolder = subfolder
        self.category = category
        self.size_bytes = size_bytes
        self.sha256 = sha256
        self.content_type = content_type

    def manifest_entry(self) -> dict[str, object]:
        return {
            "output_id": self.output_id,
            "filename": self.filename,
            "subfolder": self.subfolder,
            "type": "output",
            "category": self.category,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "content_type": self.content_type,
        }


class WorkerOutputDownload:
    """An output file opened only after assignment and path authorization."""

    def __init__(self, *, output: WorkerOutput, file: BinaryIO) -> None:
        self.output = output
        self.file = file


class WorkerExecution:
    """The single workflow execution owned by one worker assignment."""

    def __init__(
        self,
        *,
        execution_id: str,
        assignment_id: str,
        user_id: str,
        workspace_id: str,
        job_id: str,
        affinity_key: str,
        workflow_sha256: str,
    ) -> None:
        self.execution_id = execution_id
        self.assignment_id = assignment_id
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.job_id = job_id
        self.affinity_key = affinity_key
        self.workflow_sha256 = workflow_sha256
        self.state = "submitting"
        # ``state`` may temporarily contain controller-only recovery states.
        # Keep the last provider-observed queue state for the bounded wire
        # protocol, whose status enum intentionally has no such states.
        self.last_provider_state = "queued"
        self.prompt_id: str | None = None
        self.outputs: dict[str, object] | None = None
        # This inventory is deliberately private.  The wire result contains
        # only metadata; the download endpoint resolves an output ID back to
        # this verified instance-local path after re-authenticating the full
        # assignment identity.
        self.output_inventory: dict[str, WorkerOutput] = {}
        self.result_observed = False
        self.error_code: str | None = None
        self.error: str | None = None
        self.interrupt_sent = False
        self.queue_removal_sent = False
        self.cancel_requested = False
        self.cancel_requested_at: int | None = None
        self.cancel_empty_observations = 0


def _protocol_lifecycle() -> object:
    lifecycle = _WORKER_LIFECYCLE
    if lifecycle is None:
        raise WorkerProtocolUnavailable("worker lifecycle module is unavailable")
    return lifecycle


def _now_millis() -> int:
    return int(time.time() * 1000)


def _valid_identifier(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or "\x00" in value
    ):
        raise WorkerProtocolError(f"invalid worker {field}")
    return value


def _wire_execution_status(execution: WorkerExecution) -> str:
    """Map controller-only transient states to the public execution enum."""

    if execution.state in PUBLIC_EXECUTION_STATES:
        return execution.state
    if execution.state in {"running", "cancelling"}:
        return execution.last_provider_state
    # ``submitting`` and ``submission_unknown`` are deliberately non-terminal
    # internally.  Expose them as queued so callers cannot complete or reuse
    # an execution merely because its submit response was ambiguous.
    return "queued"


_INSTANCE_ROOT_PARENT = Path("/tmp/comfy-runtime")
_INSTANCE_ID_PATTERN = re.compile(r"(?:default|inst_[A-Za-z0-9]+)\Z")


def validate_instance_root(value: str | os.PathLike[str]) -> Path:
    """Validate the fixed disposable root before any deletion is allowed."""

    root = Path(value)
    if (
        not root.is_absolute()
        or root.parent != _INSTANCE_ROOT_PARENT
        or not _INSTANCE_ID_PATTERN.fullmatch(root.name)
        or root.is_symlink()
    ):
        raise WorkerProtocolError(
            "COMFY_POD_INSTANCE_ROOT must be /tmp/comfy-runtime/<safe-id>"
        )
    return root


def clean_instance_root(root: Path) -> None:
    """Clear only instance-local job state and recreate its four directories."""

    validated = validate_instance_root(root)
    if validated.exists() and not validated.is_dir():
        raise WorkerProtocolError("worker instance root is not a directory")
    validated.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in ("input", "output", "temp", "user"):
        child = validated / name
        if child.is_symlink() or (child.exists() and not child.is_dir()):
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child)
        child.mkdir(mode=0o700, exist_ok=True)


def _required_field(
    payload: Mapping[str, object],
    field: str,
    *aliases: str,
) -> object:
    for name in (field, *aliases):
        if name in payload:
            return payload[name]
    raise WorkerProtocolError(f"missing worker field: {field}")


def _required_identifier(
    payload: Mapping[str, object],
    field: str,
    *aliases: str,
) -> str:
    values = [
        _valid_identifier(payload[name], field)
        for name in (field, *aliases)
        if name in payload
    ]
    if not values:
        raise WorkerProtocolError(f"missing worker field: {field}")
    if any(value != values[0] for value in values[1:]):
        raise WorkerProtocolError(f"worker {field} fields disagree")
    return values[0]


def _optional_identifier(
    payload: Mapping[str, object],
    field: str,
    *aliases: str,
) -> str | None:
    values: list[str] = []
    for name in (field, *aliases):
        if name not in payload:
            continue
        value = payload[name]
        if value is None:
            if len(values) > 0:
                raise WorkerProtocolError(f"worker {field} fields disagree")
            values.append("")
            continue
        values.append(_valid_identifier(value, field))
    if not values:
        return None
    if any(value != values[0] for value in values[1:]):
        raise WorkerProtocolError(f"worker {field} fields disagree")
    return values[0] or None


def _body_bool(
    payload: Mapping[str, object],
    field: str,
    default: bool,
) -> bool:
    value = payload.get(field, default)
    if not isinstance(value, bool):
        raise WorkerProtocolError(f"worker {field} must be a boolean")
    return value


def _body_timestamp(
    payload: Mapping[str, object],
    field: str,
    *,
    default: int | None = None,
) -> int | None:
    value = payload.get(field, default)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise WorkerProtocolError(f"worker {field} must be a non-negative integer")
    return value


def _reject_unknown_fields(
    payload: Mapping[str, object],
    allowed: frozenset[str],
) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise WorkerProtocolError("unknown worker field: " + unknown[0])


_ASSIGNMENT_REQUEST_FIELDS = frozenset(
    {
        "assignment_id",
        "assignmentId",
        "assignment_token",
        "assignmentToken",
        "user_id",
        "userId",
        "workspace_id",
        "workspaceId",
        "job_id",
        "jobId",
        "affinity_key",
        "affinityKey",
        "model_plan_fingerprint",
        "modelPlanFingerprint",
    }
)


def _validate_json_tree(value: object, *, depth: int = 0) -> None:
    """Bound workflow nesting and collection cardinality before submission."""

    if depth > MAX_WORKFLOW_DEPTH:
        raise WorkerProtocolError("workflow nesting is too deep")
    if isinstance(value, Mapping):
        if len(value) > MAX_WORKFLOW_COLLECTION_ITEMS:
            raise WorkerProtocolError("workflow object is too large")
        for key, child in value.items():
            if not isinstance(key, str) or "\x00" in key or len(key) > 512:
                raise WorkerProtocolError("workflow contains an invalid key")
            _validate_json_tree(child, depth=depth + 1)
        return
    if isinstance(value, list):
        if len(value) > MAX_WORKFLOW_COLLECTION_ITEMS:
            raise WorkerProtocolError("workflow array is too large")
        for child in value:
            _validate_json_tree(child, depth=depth + 1)
        return
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str) and (
            "\x00" in value or len(value) > MAX_OUTPUT_FIELD_LENGTH * 16
        ):
            raise WorkerProtocolError("workflow contains an oversized string")
        return
    raise WorkerProtocolError("workflow contains an unsupported value")


def _canonical_workflow(value: object) -> tuple[Mapping[str, object], str]:
    if not isinstance(value, Mapping):
        raise WorkerProtocolError("worker workflow must be a JSON object")
    _validate_json_tree(value)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise WorkerProtocolError("worker workflow is not canonical JSON") from error
    if len(encoded) > MAX_WORKFLOW_BYTES:
        raise WorkerProtocolError("worker workflow exceeds size limit")
    return value, hashlib.sha256(encoded).hexdigest()


def _strict_assignment_token(
    payload: Mapping[str, object],
    header_token: str | None,
) -> str | None:
    body_token = _optional_identifier(payload, "assignment_token", "assignmentToken")
    if header_token is not None:
        header_token = _valid_identifier(header_token, "assignment token")
    if (
        header_token is not None
        and body_token is not None
        and not capability_matches(header_token, body_token)
    ):
        raise WorkerProtocolError("assignment token fields disagree")
    return header_token or body_token


def _assignment_error_response(
    error: WorkerProtocolError,
) -> tuple[int, dict[str, object]]:
    message = str(error)
    if "token" in message:
        status = 401
    elif "assignment" in message:
        status = 409
    else:
        status = 400
    return status, {"ok": False, "error": message}


def _safe_output_path(
    value: object,
    field: str,
    *,
    required: bool,
) -> str | None:
    if value is None and not required:
        return None
    if value == "" and not required:
        return None
    if not isinstance(value, str) or not value or len(value) > MAX_OUTPUT_FIELD_LENGTH:
        raise WorkerProtocolError(f"ComfyUI output {field} is invalid")
    if "\x00" in value or "\\" in value or value in {".", ".."}:
        raise WorkerProtocolError(f"ComfyUI output {field} is invalid")
    if field == "filename" and "/" in value:
        raise WorkerProtocolError("ComfyUI output filename is not a basename")
    if field == "subfolder":
        if value.startswith("/") or any(
            part in {"", ".", ".."} for part in value.split("/")
        ):
            raise WorkerProtocolError("ComfyUI output subfolder is invalid")
    return value


def _restricted_outputs(value: object) -> dict[str, object]:
    """Keep only bounded file references from ComfyUI history output."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise WorkerProtocolError("ComfyUI outputs are not an object")
    result: dict[str, object] = {}
    total_files = 0
    allowed_kinds = frozenset(
        {"images", "gifs", "audio", "videos", "video", "files"}
    )
    for raw_node_id, raw_node_output in list(value.items())[:MAX_OUTPUT_NODES]:
        node_id = _valid_identifier(raw_node_id, "output node id")
        if not isinstance(raw_node_output, Mapping):
            continue
        node_result: dict[str, list[dict[str, str]]] = {}
        for kind in allowed_kinds:
            raw_files = raw_node_output.get(kind)
            if raw_files is None:
                continue
            if not isinstance(raw_files, list):
                raise WorkerProtocolError("ComfyUI output collection is invalid")
            files: list[dict[str, str]] = []
            for raw_file in raw_files[: MAX_OUTPUT_FILES - total_files]:
                if not isinstance(raw_file, Mapping):
                    continue
                output_type = raw_file.get("type", "output")
                if output_type not in {"input", "output", "temp"}:
                    continue
                if output_type != "output":
                    # Input and temp references are useful to ComfyUI's
                    # history API but are not durable worker outputs.  Do
                    # not carry them into the terminal manifest or let them
                    # consume the output cardinality budget.
                    continue
                filename = _safe_output_path(
                    raw_file.get("filename"), "filename", required=True
                )
                subfolder = _safe_output_path(
                    raw_file.get("subfolder", ""), "subfolder", required=False
                )
                entry = {"filename": filename, "type": output_type}
                if subfolder:
                    entry["subfolder"] = subfolder
                files.append(entry)
                total_files += 1
                if total_files >= MAX_OUTPUT_FILES:
                    break
            if files:
                node_result[kind] = files
            if total_files >= MAX_OUTPUT_FILES:
                break
        if node_result:
            result[node_id] = node_result
        if total_files >= MAX_OUTPUT_FILES:
            break
    return result


_OUTPUT_CATEGORIES = ("images", "gifs", "audio", "videos", "video", "files")
_OUTPUT_ID_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


def _required_output_id(payload: Mapping[str, object]) -> str:
    output_id = _required_identifier(payload, "output_id", "outputId")
    if _OUTPUT_ID_PATTERN.fullmatch(output_id) is None:
        raise WorkerProtocolError("invalid worker output id")
    return output_id


def _output_id(
    execution_id: str,
    node_id: str,
    category: str,
    subfolder: str,
    filename: str,
) -> str:
    """Derive a stable opaque ID for one execution-scoped output reference."""

    identity = "\x00".join(
        (execution_id, node_id, category, subfolder, filename)
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


def _validated_output_path(
    instance_root: Path,
    *,
    filename: str,
    subfolder: str,
) -> Path:
    """Resolve an output reference without following any filesystem links."""

    try:
        instance_root = validate_instance_root(instance_root)
        instance_stat = instance_root.lstat()
    except (OSError, ValueError, WorkerProtocolError) as error:
        raise WorkerProtocolError("ComfyUI instance root is unavailable") from error
    if stat.S_ISLNK(instance_stat.st_mode) or not stat.S_ISDIR(instance_stat.st_mode):
        raise WorkerProtocolError("ComfyUI instance root is invalid")
    output_root = instance_root / "output"
    try:
        root_stat = output_root.lstat()
    except (OSError, ValueError) as error:
        raise WorkerProtocolError("ComfyUI output root is unavailable") from error
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise WorkerProtocolError("ComfyUI output root is invalid")

    relative = Path(subfolder) / filename if subfolder else Path(filename)
    candidate = output_root / relative
    try:
        parts = candidate.relative_to(output_root).parts
    except ValueError as error:
        raise WorkerProtocolError("ComfyUI output path escapes instance root") from error
    current = output_root
    try:
        for index, part in enumerate(parts):
            current = current / part
            entry_stat = current.lstat()
            if stat.S_ISLNK(entry_stat.st_mode):
                raise WorkerProtocolError("ComfyUI output path contains a symlink")
            if index < len(parts) - 1:
                if not stat.S_ISDIR(entry_stat.st_mode):
                    raise WorkerProtocolError("ComfyUI output parent is not a directory")
            elif not stat.S_ISREG(entry_stat.st_mode):
                raise WorkerProtocolError("ComfyUI output is not a regular file")
        resolved_root = output_root.resolve(strict=True)
        resolved_candidate = current.resolve(strict=True)
        resolved_candidate.relative_to(resolved_root)
    except WorkerProtocolError:
        raise
    except (OSError, ValueError) as error:
        raise WorkerProtocolError("ComfyUI output file is unavailable") from error
    return current


def _open_verified_output(
    path: Path,
    *,
    expected_size: int | None = None,
    instance_root: Path | None = None,
    subfolder: str | None = None,
    filename: str | None = None,
) -> BinaryIO:
    """Open a previously authorized output without following filesystem links."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    descriptor: int
    opened_directories: list[int] = []
    try:
        if instance_root is None:
            descriptor = os.open(path, flags | nofollow)
        else:
            if subfolder is None or filename is None:
                raise WorkerProtocolError("ComfyUI output reference is incomplete")
            instance_root = validate_instance_root(instance_root)
            # Open each path component relative to a no-following directory
            # descriptor.  This closes the lstat/open race for both nested
            # subfolders and the final output file.
            parent_descriptor = os.open(
                _INSTANCE_ROOT_PARENT,
                flags | nofollow | directory,
            )
            opened_directories.append(parent_descriptor)
            root_descriptor = os.open(
                instance_root.name,
                flags | nofollow | directory,
                dir_fd=parent_descriptor,
            )
            opened_directories.append(root_descriptor)
            output_descriptor = os.open(
                "output",
                flags | nofollow | directory,
                dir_fd=root_descriptor,
            )
            opened_directories.append(output_descriptor)
            current_descriptor = output_descriptor
            for part in subfolder.split("/") if subfolder else ():
                next_descriptor = os.open(
                    part,
                    flags | nofollow | directory,
                    dir_fd=current_descriptor,
                )
                opened_directories.append(next_descriptor)
                current_descriptor = next_descriptor
            descriptor = os.open(
                filename,
                flags | nofollow,
                dir_fd=current_descriptor,
            )
    except WorkerProtocolError:
        for opened in reversed(opened_directories):
            os.close(opened)
        raise
    except (OSError, ValueError) as error:
        for opened in reversed(opened_directories):
            os.close(opened)
        raise WorkerProtocolError("ComfyUI output file is unavailable") from error
    else:
        for opened in reversed(opened_directories):
            os.close(opened)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise WorkerProtocolError("ComfyUI output is not a regular file")
        if expected_size is not None and metadata.st_size != expected_size:
            raise WorkerProtocolError("ComfyUI output size changed")
        return os.fdopen(descriptor, "rb", buffering=0)
    except WorkerProtocolError:
        os.close(descriptor)
        raise
    except (OSError, ValueError) as error:
        os.close(descriptor)
        raise WorkerProtocolError("ComfyUI output file is unavailable") from error


def _inspect_output_file(
    instance_root: Path,
    *,
    filename: str,
    subfolder: str,
) -> tuple[Path, int, str]:
    """Read bounded output metadata from a safely opened regular file."""

    path = _validated_output_path(
        instance_root,
        filename=filename,
        subfolder=subfolder,
    )
    handle = _open_verified_output(
        path,
        instance_root=instance_root,
        subfolder=subfolder,
        filename=filename,
    )
    try:
        metadata = os.fstat(handle.fileno())
        size_bytes = metadata.st_size
        if size_bytes < 0 or size_bytes > MAX_OUTPUT_FILE_BYTES:
            raise WorkerProtocolError("ComfyUI output file exceeds the size limit")
        digest = hashlib.sha256()
        while True:
            chunk = handle.read(OUTPUT_STREAM_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
        final_metadata = os.fstat(handle.fileno())
        if final_metadata.st_size != size_bytes:
            raise WorkerProtocolError("ComfyUI output file changed while hashing")
        return path, size_bytes, digest.hexdigest()
    except WorkerProtocolError:
        raise
    except (OSError, ValueError) as error:
        raise WorkerProtocolError("ComfyUI output file could not be hashed") from error
    finally:
        handle.close()


def _materialize_output_manifest(
    restricted: Mapping[str, object],
    *,
    execution_id: str,
    instance_root: Path | None,
) -> tuple[dict[str, object], dict[str, WorkerOutput]]:
    """Attach verified metadata and paths to Comfy's bounded output refs."""

    manifest: dict[str, object] = {}
    inventory: dict[str, WorkerOutput] = {}
    total_bytes = 0
    for raw_node_id, raw_node_output in restricted.items():
        node_id = _valid_identifier(raw_node_id, "output node id")
        if not isinstance(raw_node_output, Mapping):
            continue
        node_manifest: dict[str, list[dict[str, object]]] = {}
        for category in _OUTPUT_CATEGORIES:
            raw_files = raw_node_output.get(category)
            if not isinstance(raw_files, list):
                continue
            category_manifest: list[dict[str, object]] = []
            for raw_file in raw_files:
                if not isinstance(raw_file, Mapping):
                    continue
                if raw_file.get("type", "output") != "output":
                    # Input and temp files are intentionally not durable
                    # outputs and cannot be fetched through this endpoint.
                    continue
                filename = _safe_output_path(
                    raw_file.get("filename"), "filename", required=True
                )
                subfolder_value = _safe_output_path(
                    raw_file.get("subfolder", ""), "subfolder", required=False
                )
                subfolder = subfolder_value or ""
                output_id = _output_id(
                    execution_id,
                    node_id,
                    category,
                    subfolder,
                    filename,
                )
                output = inventory.get(output_id)
                if output is None:
                    if instance_root is None:
                        raise WorkerProtocolError(
                            "worker instance root is not configured"
                        )
                    path, size_bytes, sha256 = _inspect_output_file(
                        instance_root,
                        filename=filename,
                        subfolder=subfolder,
                    )
                    if total_bytes + size_bytes > MAX_OUTPUT_TOTAL_BYTES:
                        raise WorkerProtocolError(
                            "ComfyUI execution outputs exceed the size limit"
                        )
                    content_type = (
                        mimetypes.guess_type(filename, strict=False)[0]
                        or "application/octet-stream"
                    )
                    output = WorkerOutput(
                        output_id=output_id,
                        path=path,
                        filename=filename,
                        subfolder=subfolder,
                        category=category,
                        size_bytes=size_bytes,
                        sha256=sha256,
                        content_type=content_type,
                    )
                    inventory[output_id] = output
                    total_bytes += size_bytes
                category_manifest.append(output.manifest_entry())
            if category_manifest:
                node_manifest[category] = category_manifest
        if node_manifest:
            manifest[node_id] = node_manifest
    return manifest, inventory


def _history_entry(history: object, prompt_id: str) -> Mapping[str, object] | None:
    if not isinstance(history, Mapping):
        raise WorkerProtocolError("ComfyUI history is not an object")
    if not history:
        return None
    candidate = history.get(prompt_id)
    if candidate is None and ("status" in history or "outputs" in history):
        candidate = history
    if candidate is None:
        return None
    if not isinstance(candidate, Mapping):
        raise WorkerProtocolError("ComfyUI history entry is not an object")
    return candidate


def _history_observation(
    entry: Mapping[str, object],
) -> tuple[str, dict[str, object], str | None]:
    status_value = entry.get("status")
    status: Mapping[str, object] = (
        status_value if isinstance(status_value, Mapping) else {}
    )
    status_name = status.get("status_str", status.get("status"))
    status_name = status_name.lower() if isinstance(status_name, str) else ""
    completed = status.get("completed") is True
    if status_name in {"success", "completed", "complete"} or (
        completed
        and status_name not in {"error", "failed", "cancelled", "canceled"}
    ):
        return "completed", _restricted_outputs(entry.get("outputs")), None
    if status_name in {"error", "failed", "failure"}:
        # Provider messages often contain absolute paths, model names, or
        # custom-node exception details.  The control plane needs only a
        # stable classification; raw diagnostics stay in Pod logs.
        return "failed", {}, "ComfyUI execution failed"
    if status_name in {"cancelled", "canceled", "interrupted"}:
        return "cancelled", {}, None
    if status_name in {"running", "executing", "in_progress"}:
        return "running", {}, None
    if status_name in {
        "queued",
        "pending",
        "waiting",
        "created",
        "submitted",
        "in_queue",
    }:
        return "queued", {}, None
    # An entry with an unrecognized status is evidence that ComfyUI still
    # retains something for this prompt, but it is not safe to infer either a
    # terminal cancellation or a provider queue state from it.
    return "unknown", {}, None


def _queue_prompt_state(queue: object, prompt_id: str) -> str | None:
    """Return the exact Comfy queue state for ``prompt_id`` if present.

    ComfyUI versions have emitted both mapping entries and positional queue
    records.  Only the documented prompt-id fields/positions are inspected;
    arbitrary nested strings are never searched, avoiding false matches from
    workflow content or metadata.
    """

    if not isinstance(queue, Mapping):
        raise WorkerProtocolError("ComfyUI queue is not an object")
    for field, state in (("queue_running", "running"), ("queue_pending", "queued")):
        entries = queue.get(field)
        if not isinstance(entries, list):
            raise WorkerProtocolError(f"ComfyUI queue {field} is not an array")
        for entry in entries:
            if isinstance(entry, Mapping):
                candidates = (entry.get("prompt_id"), entry.get("promptId"))
            elif isinstance(entry, (list, tuple)):
                candidates = (
                    entry[1] if len(entry) > 1 else None,
                    entry[0] if entry else None,
                )
            else:
                raise WorkerProtocolError("ComfyUI queue entry is invalid")
            if any(candidate == prompt_id for candidate in candidates):
                return state
    return None


_AFFINITY_KEY_FIELDS = (
    "affinity_key",
    "affinityKey",
    "model_plan_fingerprint",
    "modelPlanFingerprint",
)


def _required_affinity_key(payload: Mapping[str, object]) -> str:
    """Read the canonical opaque warm-affinity key from a request.

    ``model_plan_fingerprint`` is accepted as a compatibility spelling, but
    if both spellings are present they must agree.  The key is deliberately
    opaque to the Pod; it is not a capability and must never be used as one.
    """

    values: list[str] = []
    for field in _AFFINITY_KEY_FIELDS:
        if field in payload:
            values.append(_valid_identifier(payload[field], "affinity key"))
    if not values:
        raise WorkerProtocolError(
            "missing worker field: affinity_key (model_plan_fingerprint)"
        )
    if any(value != values[0] for value in values[1:]):
        raise WorkerProtocolError("worker affinity key fields disagree")
    return values[0]


def _optional_affinity_key(payload: Mapping[str, object]) -> str | None:
    if not any(field in payload for field in _AFFINITY_KEY_FIELDS):
        return None
    return _required_affinity_key(payload)


class WorkerController:
    """Provider-neutral, single-assignment controller for one Pod process.

    The controller deliberately has no provider client and no shell execution
    path.  A reset failure may invoke an injected restart callback, but a
    successful callback never promotes the process to ``ready`` on its own;
    only a fresh boot/health path may do that.
    """

    def __init__(
        self,
        worker_id: str,
        *,
        initial_state: str = "starting",
        baseline: Mapping[str, object] | None = None,
        barrier_factory: Callable[[], object] | None = None,
        reset_barrier: object | None = None,
        restart_callback: RestartCallback | None = None,
        baseline_probe: Callable[[], object | Awaitable[object]] | None = None,
        restart_probe: Callable[[], object | Awaitable[object]] | None = None,
        restart_generation_path: str | None = None,
        instance_root: str | None = None,
        comfy_client: ComfyExecutionClient | None = None,
        restart_probe_timeout_seconds: float = 30.0,
        reset_timeout_seconds: float = RESET_TOTAL_TIMEOUT_SECONDS,
        now_fn: Callable[[], int] = _now_millis,
    ) -> None:
        lifecycle = _protocol_lifecycle()
        self._worker_id = _valid_identifier(worker_id, "id")
        try:
            state_type = lifecycle.WorkerState
            state = state_type(initial_state)
        except (AttributeError, ValueError) as error:
            raise WorkerProtocolError("invalid worker initial state") from error
        self._record = lifecycle.WorkerRecord(self._worker_id, state)
        self._assignment: WorkerAssignment | None = None
        self._baseline = copy.deepcopy(dict(baseline)) if baseline is not None else None
        self._barrier_factory = barrier_factory
        self._reset_barrier = reset_barrier
        self._restart_callback = restart_callback
        self._baseline_probe = baseline_probe
        self._restart_probe = restart_probe
        if restart_generation_path is None:
            configured_generation_path = os.environ.get(
                "COMFY_POD_SUPERVISOR_GENERATION_FILE", ""
            )
        else:
            configured_generation_path = restart_generation_path
        self._restart_generation_path = (
            Path(configured_generation_path)
            if configured_generation_path
            else None
        )
        configured_instance_root = (
            os.environ.get("COMFY_POD_INSTANCE_ROOT", "")
            if instance_root is None
            else instance_root
        )
        self._instance_root = (
            validate_instance_root(configured_instance_root)
            if configured_instance_root
            else None
        )
        if restart_probe_timeout_seconds <= 0:
            raise WorkerProtocolError("restart probe timeout must be positive")
        if reset_timeout_seconds <= 0 or reset_timeout_seconds >= 60:
            raise WorkerProtocolError(
                "reset timeout must be positive and less than 60 seconds"
            )
        self._restart_probe_timeout_seconds = restart_probe_timeout_seconds
        self._reset_timeout_seconds = reset_timeout_seconds
        self._now = now_fn
        self._comfy_client = comfy_client or ComfyHTTPExecutionClient()
        self._execution: WorkerExecution | None = None
        self._lock = asyncio.Lock()

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def state(self) -> str:
        return self._record.state.value

    @property
    def assignment(self) -> WorkerAssignment | None:
        return self._assignment

    def _public_assignment(self) -> dict[str, object] | None:
        assignment = self._assignment
        if assignment is None:
            return None
        return {
            "assignment_id": assignment.assignment_id,
            "user_id": assignment.user_id,
            "workspace_id": assignment.workspace_id,
            "job_id": assignment.job_id,
            # Safe to expose: this is an opaque execution fingerprint, never
            # the assignment capability token.
            "affinity_key": assignment.affinity_key,
            "status": assignment.status,
            "completion_status": assignment.completion_status,
            "created_at": assignment.created_at,
            "heartbeat_at": assignment.heartbeat_at,
            "warm_until": assignment.warm_until,
        }

    def _public_execution(self) -> dict[str, object] | None:
        execution = self._execution
        if execution is None:
            return None
        return {
            "execution_id": execution.execution_id,
            "assignment_id": execution.assignment_id,
            "job_id": execution.job_id,
            "state": execution.state,
            "terminal": execution.state in TERMINAL_EXECUTION_STATES,
        }

    def status_payload(self) -> dict[str, object]:
        affinity = self._record.affinity
        state = self.state
        return {
            "ok": state not in {"error", "terminating"},
            "protocol": "comfy-pod-worker/v1",
            "worker_id": self._worker_id,
            "state": state,
            "ready": state == "ready",
            "affinity": (
                {
                    "user_id": affinity.user_id,
                    "workspace_id": affinity.workspace_id,
                    "affinity_key": affinity.affinity_key,
                }
                if affinity is not None
                else None
            ),
            "assignment": self._public_assignment(),
            "execution": self._public_execution(),
            "baseline_configured": self._baseline is not None,
        }

    def _transition(
        self,
        event_name: str,
        *,
        user_id: str | None = None,
        workspace_id: str | None = None,
        affinity_key: str | None = None,
    ) -> None:
        lifecycle = _protocol_lifecycle()
        event = lifecycle.WorkerEvent(event_name)
        self._record = lifecycle.transition_worker(
            self._record,
            event,
            user_id=user_id,
            workspace_id=workspace_id,
            affinity_key=affinity_key,
        )

    def mark_boot_succeeded(self) -> dict[str, object]:
        """Promote a starting/restarting process only after external readiness."""

        self._transition("boot_succeeded")
        return self.status_payload()

    def mark_boot_failed(self) -> dict[str, object]:
        self._transition("boot_failed")
        return self.status_payload()

    async def _probe_baseline(self) -> Mapping[str, object]:
        probe = self._baseline_probe
        if probe is None:
            transport = ComfyHTTPTransport(upstream_base_url())

            def request_stats() -> object:
                response = transport.request(
                    "GET",
                    "/system_stats",
                    timeout_seconds=3.0,
                )
                if getattr(response, "status_code", 0) < 200 or getattr(
                    response, "status_code", 0
                ) >= 300:
                    raise RuntimeError("ComfyUI system_stats probe failed")
                return getattr(response, "body", None)

            value = await asyncio.to_thread(request_stats)
        else:
            value = probe()
            if inspect.isawaitable(value):
                value = await value
        if not isinstance(value, Mapping):
            raise WorkerProtocolError("ComfyUI system_stats baseline is not an object")
        return copy.deepcopy(dict(value))

    async def initialize(self) -> dict[str, object]:
        """Capture a clean-start stats baseline before advertising readiness."""

        async with self._lock:
            if self.state != "starting":
                return self.status_payload()
            try:
                self._baseline = await self._probe_baseline()
                self._transition("boot_succeeded")
            except Exception:
                self._baseline = None
                self._transition("boot_failed")
            return self.status_payload()

    def _read_restart_generation(self) -> str | None:
        path = self._restart_generation_path
        if path is None:
            return None
        try:
            value = path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return None
        return value or None

    async def _recover_after_restart(
        self,
        previous_generation: str | None,
        deadline: float | None = None,
    ) -> bool:
        """Probe a restarted process and capture its new clean baseline."""

        probe = self._restart_probe or self._baseline_probe
        probe_deadline = time.monotonic() + self._restart_probe_timeout_seconds
        if deadline is not None:
            probe_deadline = min(probe_deadline, deadline)
        while True:
            if time.monotonic() >= probe_deadline:
                return False
            try:
                current_generation = self._read_restart_generation()
                if self._restart_generation_path is not None and (
                    current_generation is None
                    or current_generation == previous_generation
                ):
                    raise RuntimeError("waiting for supervisor restart acknowledgement")
                if probe is None:
                    value = await self._await_until_deadline(
                        self._probe_baseline(),
                        probe_deadline,
                    )
                else:
                    value = await self._run_sync_until_deadline(
                        probe,
                        operation_deadline=probe_deadline,
                    )
                    if inspect.isawaitable(value):
                        value = await self._await_until_deadline(
                            value,
                            probe_deadline,
                        )
                    if not isinstance(value, Mapping):
                        raise WorkerProtocolError(
                            "ComfyUI restart probe returned a non-object baseline"
                        )
                    value = copy.deepcopy(dict(value))
                self._baseline = value
                return True
            except Exception:
                if time.monotonic() >= probe_deadline:
                    return False
                await asyncio.sleep(
                    min(0.25, max(0.0, probe_deadline - time.monotonic()))
                )

    async def _recover_to_ready_after_restart(
        self,
        previous_generation: str | None,
        deadline: float | None = None,
    ) -> bool:
        if not await self._recover_after_restart(previous_generation, deadline):
            try:
                if self.state == "restarting":
                    self._transition("worker_failed")
            except Exception:
                pass
            return False
        try:
            if self.state != "restarting":
                return False
            if self._instance_root is None:
                raise WorkerProtocolError("worker instance root is not configured")
            if deadline is None:
                await asyncio.to_thread(clean_instance_root, self._instance_root)
            else:
                await self._run_sync_until_deadline(
                    clean_instance_root,
                    self._instance_root,
                    operation_deadline=deadline,
                )
            self._transition("boot_succeeded")
            self._assignment = None
            self._execution = None
            return True
        except Exception:
            try:
                if self.state == "restarting":
                    self._transition("worker_failed")
            except Exception:
                pass
            return False

    def _check_assignment(
        self,
        payload: Mapping[str, object],
        assignment_token: str | None,
    ) -> WorkerAssignment:
        assignment = self._assignment
        if assignment is None:
            raise WorkerProtocolError("worker has no active assignment")
        assignment_id = _required_identifier(payload, "assignment_id", "assignmentId")
        job_id = _required_identifier(payload, "job_id", "jobId")
        user_id = _required_identifier(payload, "user_id", "userId")
        workspace_id = _required_identifier(payload, "workspace_id", "workspaceId")
        affinity_key = _required_affinity_key(payload)
        supplied = _strict_assignment_token(payload, assignment_token)
        if supplied is None or not capability_matches(supplied, assignment.assignment_token):
            raise WorkerProtocolError("invalid assignment token")
        if (
            assignment.assignment_id != assignment_id
            or assignment.job_id != job_id
            or assignment.user_id != user_id
            or assignment.workspace_id != workspace_id
            or assignment.affinity_key != affinity_key
        ):
            raise WorkerProtocolError("assignment identity does not match")
        return assignment

    def _check_execution_assignment(
        self,
        payload: Mapping[str, object],
        assignment_token: str | None,
    ) -> WorkerAssignment:
        """Require every identity component for an execution RPC.

        Heartbeats and reset retain their older compact request shape.  The
        execution protocol is deliberately stricter: a valid worker
        capability alone is never enough to submit, inspect, or cancel a
        workflow.  All five assignment-bound fields must match the immutable
        claim held by this process.
        """

        assignment_id = _required_identifier(payload, "assignment_id", "assignmentId")
        job_id = _required_identifier(payload, "job_id", "jobId")
        user_id = _required_identifier(payload, "user_id", "userId")
        workspace_id = _required_identifier(payload, "workspace_id", "workspaceId")
        affinity_key = _required_affinity_key(payload)
        token = _strict_assignment_token(payload, assignment_token)
        assignment = self._assignment
        if assignment is None:
            raise WorkerProtocolError("worker has no active assignment")
        if token is None or not capability_matches(token, assignment.assignment_token):
            raise WorkerProtocolError("invalid assignment token")
        if (
            assignment.assignment_id != assignment_id
            or assignment.job_id != job_id
            or assignment.user_id != user_id
            or assignment.workspace_id != workspace_id
            or assignment.affinity_key != affinity_key
        ):
            raise WorkerProtocolError("assignment identity does not match")
        return assignment

    @staticmethod
    async def _client_call(
        client: object,
        method_name: str,
        *args: object,
        **kwargs: object,
    ) -> object:
        method = getattr(client, method_name, None)
        if not callable(method):
            raise ComfyExecutionError("ComfyUI execution client is incomplete")
        try:
            value = method(*args, **kwargs)
            if inspect.isawaitable(value):
                value = await value
            return value
        except ComfyExecutionError:
            raise
        except Exception as error:
            raise ComfyExecutionError("ComfyUI request failed") from error

    def _execution_response(
        self,
        execution: WorkerExecution,
        *,
        kind: str,
    ) -> dict[str, object]:
        result: dict[str, object] | None = None
        if execution.state == "completed":
            result = {"outputs": copy.deepcopy(execution.outputs or {})}
        response: dict[str, object] = {
            **self.status_payload(),
            "kind": kind,
            "execution": {
                "execution_id": execution.execution_id,
                "status": _wire_execution_status(execution),
                "result": result,
                "error_code": execution.error_code,
                "error_message": execution.error,
            },
        }
        return response

    async def execute(
        self,
        payload: Mapping[str, object],
        assignment_token: str | None,
    ) -> tuple[int, dict[str, object]]:
        async with self._lock:
            try:
                assignment = self._check_execution_assignment(payload, assignment_token)
            except WorkerProtocolError as error:
                return _assignment_error_response(error)
            if self.state != "busy" or assignment.completion_status is not None:
                return 409, {
                    "ok": False,
                    "error": "assignment is not active",
                    "state": self.state,
                }
            execution_id = _required_identifier(payload, "execution_id", "executionId")
            workflow, workflow_sha256 = _canonical_workflow(
                _required_field(payload, "workflow")
            )
            current = self._execution
            if current is not None:
                if (
                    current.execution_id != execution_id
                    or current.workflow_sha256 != workflow_sha256
                ):
                    return 409, {
                        "ok": False,
                        "error": "worker already has a different execution",
                    }
                if current.state == "submission_unknown":
                    return 502, {
                        **self._execution_response(current, kind="duplicate"),
                        "ok": False,
                        "error": "ComfyUI submission status is unknown",
                    }
                return 200, self._execution_response(current, kind="duplicate")

            execution = WorkerExecution(
                execution_id=execution_id,
                assignment_id=assignment.assignment_id,
                user_id=assignment.user_id,
                workspace_id=assignment.workspace_id,
                job_id=assignment.job_id,
                affinity_key=assignment.affinity_key,
                workflow_sha256=workflow_sha256,
            )
            self._execution = execution
            client_id = "worker-" + hashlib.sha256(
                assignment.assignment_id.encode("utf-8")
            ).hexdigest()[:32]
            try:
                prompt_id = await self._client_call(
                    self._comfy_client,
                    "submit",
                    workflow,
                    client_id=client_id,
                )
                execution.prompt_id = _valid_identifier(prompt_id, "prompt id")
                execution.state = "queued"
            except ComfySubmissionRejected:
                # A definitive provider rejection is terminal and can be
                # completed as failed without risking a duplicate submission.
                execution.state = "failed"
                execution.error_code = "COMFY_SUBMIT_REJECTED"
                execution.error = "ComfyUI execution submission failed"
                return 502, self._execution_response(execution, kind="accepted")
            except (ComfySubmissionUnknown, ComfyExecutionError):
                # The submission result may be ambiguous, so never retry the
                # same execution id automatically.  Keep an isolated local
                # state: complete/reuse are forbidden until reset drains or
                # restarts the worker and establishes a clean baseline.
                execution.state = "submission_unknown"
                execution.error_code = "COMFY_SUBMIT_UNKNOWN"
                execution.error = "ComfyUI submission status is unknown"
                return 502, {
                    **self._execution_response(execution, kind="accepted"),
                    "ok": False,
                    "error": "ComfyUI submission status is unknown",
                }
            return 200, self._execution_response(execution, kind="accepted")

    async def result(
        self,
        payload: Mapping[str, object],
        assignment_token: str | None,
    ) -> tuple[int, dict[str, object]]:
        async with self._lock:
            try:
                self._check_execution_assignment(payload, assignment_token)
            except WorkerProtocolError as error:
                return _assignment_error_response(error)
            execution_id = _required_identifier(payload, "execution_id", "executionId")
            execution = self._execution
            if execution is None or execution.execution_id != execution_id:
                return 404, {"ok": False, "error": "execution not found"}
            if execution.state in TERMINAL_EXECUTION_STATES:
                kind = (
                    "duplicate"
                    if execution.result_observed
                    else execution.state
                )
                execution.result_observed = True
                return 200, self._execution_response(execution, kind=kind)
            prompt_id = execution.prompt_id
            if prompt_id is None:
                if execution.state == "submission_unknown":
                    return 503, {
                        **self._execution_response(execution, kind="pending"),
                        "ok": False,
                        "error": "ComfyUI submission status is unknown",
                    }
                return 503, {
                    **self._execution_response(execution, kind="pending"),
                    "ok": False,
                    "error": "execution has no provider id",
                }
            try:
                history = await self._client_call(
                    self._comfy_client,
                    "history",
                    prompt_id,
                )
                entry = _history_entry(history, prompt_id)
                if entry is None:
                    if not execution.cancel_requested:
                        execution.state = "queued"
                        execution.last_provider_state = "queued"
                    else:
                        queue = await self._client_call(
                            self._comfy_client,
                            "queue",
                        )
                        queue_state = _queue_prompt_state(queue, prompt_id)
                        if queue_state is not None:
                            execution.cancel_empty_observations = 0
                            execution.last_provider_state = queue_state
                            execution.state = "cancelling"
                        else:
                            execution.cancel_empty_observations += 1
                            if (
                                execution.cancel_empty_observations
                                >= MIN_CANCEL_QUEUE_CONFIRMATIONS
                            ):
                                execution.state = "cancelled"
                                execution.error_code = None
                                execution.error = None
                            else:
                                execution.state = "cancelling"
                else:
                    state, outputs, error = _history_observation(entry)
                    if state in TERMINAL_EXECUTION_STATES:
                        if state == "completed":
                            materialized_outputs, output_inventory = await asyncio.to_thread(
                                _materialize_output_manifest,
                                outputs,
                                execution_id=execution.execution_id,
                                instance_root=self._instance_root,
                            )
                            execution.outputs = materialized_outputs
                            execution.output_inventory = output_inventory
                        else:
                            execution.outputs = {}
                            execution.output_inventory = {}
                        execution.state = state
                        execution.error = error
                        execution.error_code = (
                            "COMFY_EXECUTION_FAILED" if state == "failed" else None
                        )
                    else:
                        execution.last_provider_state = state
                        if state == "unknown":
                            # Unknown history is fail-closed evidence of
                            # retained provider state.  Even an empty queue
                            # cannot prove cancellation while this entry
                            # remains unclassified.
                            execution.last_provider_state = "queued"
                            if execution.cancel_requested:
                                queue = await self._client_call(
                                    self._comfy_client,
                                    "queue",
                                )
                                queue_state = _queue_prompt_state(queue, prompt_id)
                                if queue_state is not None:
                                    execution.last_provider_state = queue_state
                                execution.cancel_empty_observations = 0
                                execution.state = "cancelling"
                            else:
                                execution.state = "queued"
                        elif not execution.cancel_requested:
                            execution.state = state
                        else:
                            queue = await self._client_call(
                                self._comfy_client,
                                "queue",
                            )
                            queue_state = _queue_prompt_state(queue, prompt_id)
                            if queue_state is not None:
                                execution.cancel_empty_observations = 0
                                execution.last_provider_state = queue_state
                                execution.state = "cancelling"
                            else:
                                execution.cancel_empty_observations += 1
                                if (
                                    execution.cancel_empty_observations
                                    >= MIN_CANCEL_QUEUE_CONFIRMATIONS
                                ):
                                    execution.state = "cancelled"
                                    execution.error_code = None
                                    execution.error = None
                                else:
                                    execution.state = "cancelling"
            except WorkerProtocolError:
                return 502, {"ok": False, "error": "invalid ComfyUI result"}
            except Exception:
                return 502, {"ok": False, "error": "ComfyUI result unavailable"}
            kind = "pending" if execution.state not in TERMINAL_EXECUTION_STATES else execution.state
            if execution.state in TERMINAL_EXECUTION_STATES:
                execution.result_observed = True
            return 200, self._execution_response(execution, kind=kind)

    async def open_output(
        self,
        payload: Mapping[str, object],
        assignment_token: str | None,
    ) -> tuple[int, WorkerOutputDownload | None]:
        """Authorize and safely open one cached terminal output reference."""

        async with self._lock:
            try:
                self._check_execution_assignment(payload, assignment_token)
                execution_id = _required_identifier(
                    payload, "execution_id", "executionId"
                )
                output_id = _required_output_id(payload)
            except WorkerProtocolError as error:
                if str(error) == "worker has no active assignment":
                    # Reset/restart clears the in-memory inventory and its
                    # assignment atomically; old output URLs become ordinary
                    # not-found responses rather than reusable capabilities.
                    return 404, None
                return _assignment_error_response(error)[0], None
            execution = self._execution
            if (
                execution is None
                or execution.execution_id != execution_id
                or execution.state != "completed"
            ):
                return 404, None
            output = execution.output_inventory.get(output_id)
            if output is None or self._instance_root is None:
                return 404, None
            try:
                # Rebuild and revalidate the path on every download.  The
                # cached path is not itself an authorization primitive: a
                # replaced output directory, symlink, or changed file must
                # fail closed even when the output ID was previously valid.
                path = _validated_output_path(
                    self._instance_root,
                    filename=output.filename,
                    subfolder=output.subfolder,
                )
                if path != output.path:
                    return 404, None
                handle = _open_verified_output(
                    path,
                    expected_size=output.size_bytes,
                    instance_root=self._instance_root,
                    subfolder=output.subfolder,
                    filename=output.filename,
                )
            except WorkerProtocolError:
                return 404, None
            return 200, WorkerOutputDownload(output=output, file=handle)

    async def cancel(
        self,
        payload: Mapping[str, object],
        assignment_token: str | None,
    ) -> tuple[int, dict[str, object]]:
        async with self._lock:
            try:
                self._check_execution_assignment(payload, assignment_token)
            except WorkerProtocolError as error:
                return _assignment_error_response(error)
            execution_id = _required_identifier(payload, "execution_id", "executionId")
            execution = self._execution
            if execution is None or execution.execution_id != execution_id:
                return 404, {"ok": False, "error": "execution not found"}
            if execution.state in TERMINAL_EXECUTION_STATES:
                return 200, self._execution_response(execution, kind="already_terminal")
            if execution.state == "submission_unknown":
                return 409, {
                    **self._execution_response(execution, kind="cancel_requested"),
                    "ok": False,
                    "error": "execution submission status is unknown; reset required",
                }
            prompt_id = execution.prompt_id
            if prompt_id is None:
                return 409, {"ok": False, "error": "execution is not submitted"}
            if execution.cancel_requested and execution.interrupt_sent and execution.queue_removal_sent:
                return 200, self._execution_response(execution, kind="cancel_requested")
            execution.cancel_requested = True
            if execution.cancel_requested_at is None:
                execution.cancel_requested_at = self._now()
            execution.state = "cancelling"
            try:
                if not execution.interrupt_sent:
                    await self._client_call(self._comfy_client, "interrupt", prompt_id)
                    execution.interrupt_sent = True
                if not execution.queue_removal_sent:
                    await self._client_call(
                        self._comfy_client,
                        "remove_from_queue",
                        prompt_id,
                    )
                    execution.queue_removal_sent = True
            except Exception:
                # Preserve which provider calls already succeeded.  A retry
                # can finish only the missing idempotent operation, while the
                # caller still cannot complete/reset the assignment yet.
                execution.error_code = "COMFY_CANCEL_UNAVAILABLE"
                execution.error = "ComfyUI cancellation unavailable"
                return 502, {
                    **self.status_payload(),
                    "ok": False,
                    "kind": "cancel_requested",
                    "execution": {
                        "execution_id": execution.execution_id,
                        "status": _wire_execution_status(execution),
                        "result": None,
                        "error_code": execution.error_code,
                        "error_message": execution.error,
                    },
                }
            execution.error_code = None
            execution.error = None
            return 200, self._execution_response(execution, kind="cancel_requested")

    async def claim(
        self,
        payload: Mapping[str, object],
    ) -> tuple[int, dict[str, object]]:
        async with self._lock:
            assignment_id = _required_identifier(payload, "assignment_id", "assignmentId")
            user_id = _required_identifier(payload, "user_id", "userId")
            workspace_id = _required_identifier(payload, "workspace_id", "workspaceId")
            job_id = _required_identifier(payload, "job_id", "jobId")
            affinity_key = _required_affinity_key(payload)
            requested_token = _required_identifier(
                payload, "assignment_token", "assignmentToken"
            )
            now = self._now()
            current = self._assignment
            if current is not None:
                same_identity = (
                    current.assignment_id == assignment_id
                    and current.user_id == user_id
                    and current.workspace_id == workspace_id
                    and current.job_id == job_id
                    and current.affinity_key == affinity_key
                )
                if same_identity and capability_matches(
                    requested_token, current.assignment_token
                ):
                    return 200, {
                        **self.status_payload(),
                        "kind": "duplicate",
                    }
                if (
                    self.state == "user_warm"
                    and current.completion_status == "completed"
                    and current.warm_until is not None
                    and current.warm_until > now
                    and current.user_id == user_id
                    and current.workspace_id == workspace_id
                    and current.affinity_key == affinity_key
                ):
                    # The controller lock makes this replacement atomic from
                    # the worker protocol's perspective: no request can
                    # observe USER_WARM after the old terminal assignment is
                    # accepted but before the new BUSY assignment exists.
                    token = requested_token
                    self._transition(
                        "job_started",
                        user_id=user_id,
                        workspace_id=workspace_id,
                        affinity_key=affinity_key,
                    )
                    self._assignment = WorkerAssignment(
                        assignment_id=assignment_id,
                        assignment_token=token,
                        user_id=user_id,
                        workspace_id=workspace_id,
                        job_id=job_id,
                        affinity_key=affinity_key,
                        created_at=now,
                        heartbeat_at=now,
                    )
                    self._execution = None
                    return 200, {
                        **self.status_payload(),
                        "kind": "claimed",
                        "warm_reused": True,
                    }
                return 409, {
                    "ok": False,
                    "error": (
                        "worker warm affinity does not match, is expired, "
                        "or has a non-terminal assignment"
                        if self.state == "user_warm"
                        else "worker already has a different assignment"
                    ),
                    "state": self.state,
                }

            lifecycle = _protocol_lifecycle()
            state = self._record.state
            if state is lifecycle.WorkerState.USER_WARM:
                affinity = self._record.affinity
                if affinity is None or (
                    affinity.user_id != user_id
                    or affinity.workspace_id != workspace_id
                    or affinity.affinity_key != affinity_key
                ):
                    return 409, {
                        "ok": False,
                        "error": "worker has a different warm affinity; reset required",
                        "state": self.state,
                    }
            elif state is not lifecycle.WorkerState.READY:
                return 409, {
                    "ok": False,
                    "error": "worker is not ready",
                    "state": self.state,
                }

            token = requested_token
            self._transition(
                "job_started",
                user_id=user_id,
                workspace_id=workspace_id,
                affinity_key=affinity_key,
            )
            self._assignment = WorkerAssignment(
                assignment_id=assignment_id,
                assignment_token=token,
                user_id=user_id,
                workspace_id=workspace_id,
                job_id=job_id,
                affinity_key=affinity_key,
                created_at=now,
                heartbeat_at=now,
            )
            self._execution = None
            return 200, {
                **self.status_payload(),
                "kind": "claimed",
            }

    async def heartbeat(
        self,
        payload: Mapping[str, object],
        assignment_token: str | None,
    ) -> tuple[int, dict[str, object]]:
        async with self._lock:
            try:
                assignment = self._check_assignment(payload, assignment_token)
            except WorkerProtocolError as error:
                return 401 if "token" in str(error) else 409, {
                    "ok": False,
                    "error": str(error),
                }
            if self.state != "busy" or assignment.completion_status is not None:
                return 409, {
                    "ok": False,
                    "error": "assignment is not active",
                    "state": self.state,
                }
            assignment.heartbeat_at = self._now()
            assignment.status = "running"
            return 200, {**self.status_payload(), "kind": "heartbeat"}

    async def complete(
        self,
        payload: Mapping[str, object],
        assignment_token: str | None,
    ) -> tuple[int, dict[str, object]]:
        async with self._lock:
            try:
                assignment = self._check_assignment(payload, assignment_token)
            except WorkerProtocolError as error:
                return 401 if "token" in str(error) else 409, {
                    "ok": False,
                    "error": str(error),
                }
            requested_status = payload.get("status", "completed")
            if not isinstance(requested_status, str) or requested_status not in {
                "completed",
                "failed",
                "cancelled",
            }:
                return 400, {"ok": False, "error": "invalid completion status"}
            status = requested_status
            if assignment.completion_status is not None:
                if assignment.completion_status != status:
                    return 409, {
                        "ok": False,
                        "error": "conflicting completion replay",
                    }
                return 200, {
                    **self.status_payload(),
                    "kind": "duplicate",
                    "reset_required": self.state != "user_warm",
                }
            if self.state != "busy":
                return 409, {
                    "ok": False,
                    "error": "assignment is not active",
                    "state": self.state,
                }

            execution = self._execution
            if execution is None and status == "completed":
                return 409, {
                    "ok": False,
                    "error": "completed assignment requires a terminal execution",
                }
            if execution is not None:
                if execution.state not in TERMINAL_EXECUTION_STATES:
                    return 409, {
                        "ok": False,
                        "error": "execution must be terminal before complete",
                        "state": execution.state,
                    }
                expected_status = execution.state
                if status != expected_status:
                    return 409, {
                        "ok": False,
                        "error": "completion status does not match execution",
                    }

            keep_warm = _body_bool(payload, "keep_warm", False)
            warm_until = _body_timestamp(payload, "warm_until")
            now = self._now()
            if keep_warm and status != "completed":
                return 400, {
                    "ok": False,
                    "error": "failed or cancelled assignment requires cleanup",
                }
            if keep_warm and not assignment.affinity_key:
                return 400, {
                    "ok": False,
                    "error": "keep_warm requires a non-empty affinity_key",
                }
            if keep_warm and (warm_until is None or warm_until <= now):
                return 400, {
                    "ok": False,
                    "error": "keep_warm requires a future warm_until",
                }
            if not keep_warm and warm_until is not None:
                return 400, {
                    "ok": False,
                    "error": "warm_until requires keep_warm",
                }

            if status == "completed":
                self._transition("job_finished")
                if not keep_warm:
                    self._transition("reset_requested")
            else:
                self._transition("job_failed")
            assignment.completion_status = status
            assignment.status = status
            assignment.heartbeat_at = now
            assignment.warm_until = warm_until if keep_warm else None
            return 200, {
                **self.status_payload(),
                "kind": "completed",
                "reset_required": self.state != "user_warm",
            }

    def _new_barrier(self) -> object:
        if self._barrier_factory is not None:
            return self._barrier_factory()
        if self._reset_barrier is not None:
            return self._reset_barrier
        lifecycle = _protocol_lifecycle()
        return lifecycle.ComfyResetBarrier(
            ComfyHTTPTransport(upstream_base_url()),
            policy=lifecycle.CleanupPolicy(
                total_timeout_seconds=self._reset_timeout_seconds,
            ),
        )

    @staticmethod
    def _barrier_reset(
        barrier: object,
        *,
        baseline: dict[str, object],
        deadline: float,
    ) -> object:
        reset = getattr(barrier, "reset", None)
        if not callable(reset):
            raise WorkerProtocolError("worker reset barrier is incomplete")
        try:
            parameters = inspect.signature(reset).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_deadline = "deadline" in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        if accepts_deadline:
            return reset(baseline=baseline, deadline=deadline)
        # Test and legacy barriers may not yet expose the optional deadline;
        # the production ComfyResetBarrier does, and its transport enforces
        # the actual bound.  Do not retry a reset after a TypeError because a
        # provider mutation may already have happened.
        return reset(baseline=baseline)

    @staticmethod
    async def _await_until_deadline(
        awaitable: Awaitable[object],
        deadline: float,
    ) -> object:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("worker operation deadline exceeded")
        return await asyncio.wait_for(awaitable, timeout=remaining)

    @classmethod
    async def _run_sync_until_deadline(
        cls,
        callback: Callable[..., object],
        *args: object,
        operation_deadline: float,
        **kwargs: object,
    ) -> object:
        return await cls._await_until_deadline(
            asyncio.to_thread(callback, *args, **kwargs),
            operation_deadline,
        )

    async def _invoke_restart(self, deadline: float | None = None) -> bool:
        callback = self._restart_callback
        if callback is None:
            return False
        try:
            if deadline is None:
                result = await asyncio.to_thread(callback)
            else:
                result = await self._run_sync_until_deadline(
                    callback,
                    operation_deadline=deadline,
                )
            if inspect.isawaitable(result):
                if deadline is None:
                    result = await result
                else:
                    result = await self._await_until_deadline(result, deadline)
            return result is not False
        except Exception:
            return False

    async def reset(
        self,
        payload: Mapping[str, object],
        assignment_token: str | None,
    ) -> tuple[int, dict[str, object]]:
        if "baseline" in payload:
            return 400, {
                "ok": False,
                "error": "baseline is controller-owned and cannot be supplied",
            }
        async with self._lock:
            reset_deadline = time.monotonic() + self._reset_timeout_seconds
            assignment = self._assignment
            try:
                self._check_assignment(payload, assignment_token)
            except WorkerProtocolError as error:
                return _assignment_error_response(error)
            baseline = self._baseline
            if baseline is None:
                return 503, {
                    "ok": False,
                    "error_code": "RESET_BARRIER_UNAVAILABLE",
                    "error": "worker reset failed",
                }
            if assignment is None:
                return 409, {
                    "ok": False,
                    "error": "worker has no active assignment",
                }
            if assignment.completion_status is None:
                execution = self._execution
                if execution is None or execution.state != "submission_unknown":
                    return 409, {
                        "ok": False,
                        "error": "assignment must be terminal before reset",
                    }
                # The submit response may have been lost after ComfyUI
                # accepted the prompt.  Enter cleanup without declaring a
                # terminal execution; the barrier must drain/restart before
                # this worker can be reused.
                if self.state == "busy":
                    self._transition("job_failed")
                    assignment.status = "failed"
                    assignment.heartbeat_at = self._now()
                elif self.state not in {"cleaning"}:
                    # A previous reset may already have moved the worker to
                    # error/restarting.  Do not repeat the BUSY-only
                    # transition (which would itself be invalid); return a
                    # stable failure so reset retries remain idempotent.
                    return 503, {
                        **self.status_payload(),
                        "ok": False,
                        "kind": "reset_failed",
                        "error_code": "RESET_BARRIER_NOT_CLEAN",
                        "error": "worker reset failed",
                        "restart_requested": False,
                        "restart_dispatched": False,
                    }

            if self.state not in {"ready", "user_warm", "cleaning"}:
                return 409, {
                    "ok": False,
                    "error": "worker is not resettable",
                    "state": self.state,
                }
            failure_code = "RESET_BARRIER_NOT_CLEAN"
            result: object | None = None
            try:
                if self.state != "cleaning":
                    self._transition("reset_requested")
                barrier = self._new_barrier()
                result = await self._run_sync_until_deadline(
                    self._barrier_reset,
                    barrier,
                    baseline=copy.deepcopy(dict(baseline)),
                    operation_deadline=reset_deadline,
                    deadline=reset_deadline,
                )
                if inspect.isawaitable(result):
                    # A custom/legacy barrier may return an awaitable from
                    # its synchronous adapter.  Keep that second phase under
                    # the same reset deadline; otherwise an async barrier
                    # can outlive the gateway request and leave the
                    # controller lock held indefinitely.
                    result = await self._await_until_deadline(
                        result,
                        reset_deadline,
                    )
            except Exception:
                result = None
                failure_code = "RESET_BARRIER_UNAVAILABLE"
            else:
                success = bool(getattr(result, "success", False))
                baseline_ok = bool(getattr(result, "baseline_ok", False))
                queue_empty = bool(getattr(result, "queue_empty", False))
                history_empty = bool(getattr(result, "history_empty", False))
                action = getattr(getattr(result, "action", None), "value", None)
                if action is None and isinstance(
                    getattr(result, "action", None), str
                ):
                    action = getattr(result, "action")
                if success and baseline_ok and queue_empty and history_empty and action == "ready":
                    try:
                        if self._instance_root is None:
                            raise WorkerProtocolError(
                                "worker instance root is not configured"
                            )
                        await self._run_sync_until_deadline(
                            clean_instance_root,
                            self._instance_root,
                            operation_deadline=reset_deadline,
                        )
                    except Exception:
                        failure_code = "INSTANCE_CLEANUP_FAILED"
                    else:
                        self._transition("reset_succeeded")
                        self._assignment = None
                        self._execution = None
                        return 200, {
                            **self.status_payload(),
                            "kind": "reset",
                            "barrier": {
                                "success": True,
                                "baseline_ok": True,
                                "queue_empty": True,
                                "history_empty": True,
                                "instance_cleaned": True,
                            },
                        }
                if not (
                    success
                    and baseline_ok
                    and queue_empty
                    and history_empty
                    and action == "ready"
                ):
                    failure_code = "RESET_BARRIER_NOT_CLEAN"

            try:
                if self.state == "cleaning":
                    self._transition("reset_failed")
            except Exception:
                self._record = _protocol_lifecycle().WorkerRecord(
                    self._worker_id,
                    _protocol_lifecycle().WorkerState.ERROR,
                )
            action_value = getattr(getattr(result, "action", None), "value", None)
            if action_value is None and isinstance(
                getattr(result, "action", None), str
            ):
                action_value = getattr(result, "action")
            restart_requested = result is None or action_value == "request_restart"
            restarted = False
            if restart_requested:
                previous_generation = self._read_restart_generation()
                try:
                    self._transition("restart_requested")
                except Exception:
                    pass
                restarted = await self._invoke_restart(reset_deadline)
                if restarted and await self._recover_to_ready_after_restart(
                    previous_generation,
                    reset_deadline,
                ):
                    return 200, {
                        **self.status_payload(),
                        "kind": "restarted",
                        "barrier": {
                            "success": False,
                            "recovered_by_restart": True,
                        },
                    }
                failure_code = "WORKER_RESTART_FAILED"
                if not restarted:
                    try:
                        if self.state == "restarting":
                            self._transition("worker_failed")
                    except Exception:
                        pass
            return 503, {
                **self.status_payload(),
                "ok": False,
                "kind": "reset_failed",
                "error_code": failure_code,
                "error": "worker reset failed",
                "restart_requested": restart_requested,
                "restart_dispatched": restarted,
            }


async def request_body(request: web.Request) -> AsyncIterator[bytes]:
    async for chunk in request.content.iter_chunked(64 * 1024):
        yield chunk


async def relay_websocket(
    source: web.WebSocketResponse | object,
    destination: web.WebSocketResponse | object,
) -> None:
    async for message in source:
        if message.type == WSMsgType.TEXT:
            await destination.send_str(message.data)
        elif message.type == WSMsgType.BINARY:
            await destination.send_bytes(message.data)
        elif message.type == WSMsgType.PING:
            await destination.ping(message.data)
        elif message.type == WSMsgType.PONG:
            await destination.pong(message.data)
        elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
            break


async def proxy_websocket(request: web.Request) -> web.StreamResponse:
    session: ClientSession = request.app["client_session"]
    upstream_url = upstream_base_url() + request.rel_url.path_qs
    downstream = web.WebSocketResponse(autoping=True, heartbeat=30)
    await downstream.prepare(request)

    try:
        async with session.ws_connect(
            upstream_url,
            headers=forwarded_headers(request),
            autoping=True,
            heartbeat=30,
            max_msg_size=0,
        ) as upstream:
            downstream_to_upstream = asyncio.create_task(
                relay_websocket(downstream, upstream)
            )
            upstream_to_downstream = asyncio.create_task(
                relay_websocket(upstream, downstream)
            )
            done, pending = await asyncio.wait(
                {downstream_to_upstream, upstream_to_downstream},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
    finally:
        await downstream.close()
    return downstream


async def proxy_http(request: web.Request) -> web.StreamResponse:
    session: ClientSession = request.app["client_session"]
    upstream_url = upstream_base_url() + request.rel_url.path_qs
    body = request_body(request) if request.can_read_body else None
    async with session.request(
        request.method,
        upstream_url,
        headers=forwarded_headers(request),
        data=body,
        allow_redirects=False,
    ) as upstream:
        downstream = web.StreamResponse(
            status=upstream.status,
            reason=upstream.reason,
            headers=response_headers(upstream.headers),
        )
        await downstream.prepare(request)
        async for chunk in upstream.content.iter_chunked(64 * 1024):
            await downstream.write(chunk)
        await downstream.write_eof()
        return downstream


async def _json_body(request: web.Request) -> Mapping[str, object]:
    content_length = request.content_length
    if content_length is not None and content_length > MAX_WORKER_REQUEST_BYTES:
        raise WorkerRequestTooLarge("worker request body is too large")
    chunks: list[bytes] = []
    total = 0
    try:
        async for chunk in request.content.iter_chunked(64 * 1024):
            if not isinstance(chunk, bytes):
                raise WorkerProtocolError("request body could not be read")
            total += len(chunk)
            if total > MAX_WORKER_REQUEST_BYTES:
                raise WorkerRequestTooLarge("worker request body is too large")
            chunks.append(chunk)
    except WorkerRequestTooLarge:
        raise
    except (OSError, ValueError) as error:
        raise WorkerProtocolError("request body could not be read") from error
    body = b"".join(chunks)
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
        raise WorkerProtocolError("request body must be a JSON object") from error
    if not isinstance(payload, Mapping):
        raise WorkerProtocolError("request body must be a JSON object")
    return payload


def _worker_json_response(
    payload: Mapping[str, object],
    *,
    status: int = 200,
) -> web.Response:
    """Serialize a bounded worker response without leaking raw provider data."""

    try:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        body = b'{"ok":false,"error":"worker response unavailable"}'
        status = 500
    if len(body) > MAX_WORKER_RESPONSE_BYTES:
        body = b'{"ok":false,"error":"worker response too large"}'
        status = 500
    return web.Response(body=body, status=status, content_type="application/json")


def _worker_capability(request: web.Request) -> str | None:
    return _header_value(
        request,
        WORKER_CAPABILITY_HEADER,
        "X-Comfy-Pod-Worker-Capability",
        "X-Comfy-Pod-Capability",
    )


def _assignment_token(request: web.Request) -> str | None:
    return _header_value(
        request,
        ASSIGNMENT_TOKEN_HEADER,
        "X-Comfy-Assignment-Token",
    )


_OUTPUT_QUERY_FIELDS = frozenset(
    {
        "assignment_id",
        "user_id",
        "workspace_id",
        "job_id",
        "affinity_key",
        "execution_id",
        "output_id",
    }
)


def _worker_query(
    request: web.Request,
    allowed: frozenset[str],
) -> Mapping[str, object]:
    unknown = sorted(set(request.query.keys()) - allowed)
    if unknown:
        raise WorkerProtocolError("unknown worker field: " + unknown[0])
    payload: dict[str, object] = {}
    for field in sorted(allowed):
        values = request.query.getall(field, [])
        if len(values) > 1:
            raise WorkerProtocolError("worker query field is repeated: " + field)
        if values:
            payload[field] = values[0]
    return payload


async def _stream_worker_output(
    request: web.Request,
    controller: WorkerController,
) -> web.StreamResponse:
    payload = _worker_query(request, _OUTPUT_QUERY_FIELDS)
    status, download = await controller.open_output(
        payload,
        # The output transfer endpoint deliberately accepts only the
        # canonical assignment header.  The legacy alias remains available
        # for older JSON RPCs but must not silently broaden this download
        # capability.
        request.headers.get(ASSIGNMENT_TOKEN_HEADER),
    )
    if download is None:
        return _worker_json_response(
            {"ok": False, "error": "worker output unavailable"},
            status=status,
        )

    output = download.output
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Length": str(output.size_bytes),
            "Content-Type": output.content_type,
            "X-Output-ID": output.output_id,
            "X-Output-SHA256": output.sha256,
            "Cache-Control": "no-store",
        },
    )
    try:
        await response.prepare(request)
        while True:
            chunk = await asyncio.to_thread(
                download.file.read,
                OUTPUT_STREAM_CHUNK_BYTES,
            )
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise WorkerProtocolError("worker output read failed")
            await response.write(chunk)
        await response.write_eof()
    except asyncio.CancelledError:
        raise
    except Exception:
        # Headers may already be committed, so an error cannot safely become
        # a JSON response.  Close the stream and let the caller observe a
        # truncated transfer without exposing filesystem/provider details.
        response.force_close()
    finally:
        download.file.close()
    return response


async def worker_route(request: web.Request) -> web.StreamResponse:
    """Handle the opt-in worker control protocol.

    The namespace is deliberately intercepted before the ordinary proxy.  A
    disabled protocol returns 404, so a Pod with only the browser token does
    not accidentally expose lifecycle controls upstream or reveal their
    existence.
    """

    capability = request.app.get("worker_capability")
    if not isinstance(capability, str) or not capability:
        return _worker_json_response({"error": "not found"}, status=404)
    if not capability_matches(_worker_capability(request), capability):
        return _worker_json_response({"error": "unauthorized"}, status=401)

    controller = request.app.get("worker_controller")
    if controller is None:
        return _worker_json_response(
            {"ok": False, "error": "worker protocol unavailable"},
            status=503,
        )

    try:
        if request.path == f"{WORKER_PREFIX}/status" and request.method == "GET":
            return _worker_json_response(controller.status_payload())

        if request.path == OUTPUT_PATH and request.method == "GET":
            return await _stream_worker_output(request, controller)

        if request.path not in {
            f"{WORKER_PREFIX}/claim",
            f"{WORKER_PREFIX}/heartbeat",
            f"{WORKER_PREFIX}/complete",
            f"{WORKER_PREFIX}/reset",
            EXECUTION_PATH,
            RESULT_PATH,
            CANCEL_PATH,
        } or request.method != "POST":
            return _worker_json_response({"error": "not found"}, status=404)

        payload = await _json_body(request)
        if request.path.endswith("/claim"):
            _reject_unknown_fields(payload, _ASSIGNMENT_REQUEST_FIELDS)
            status, response = await controller.claim(payload)
        elif request.path.endswith("/heartbeat"):
            _reject_unknown_fields(payload, _ASSIGNMENT_REQUEST_FIELDS)
            status, response = await controller.heartbeat(
                payload,
                _assignment_token(request),
            )
        elif request.path.endswith("/complete"):
            _reject_unknown_fields(
                payload,
                _ASSIGNMENT_REQUEST_FIELDS
                | frozenset({"status", "keep_warm", "warm_until"}),
            )
            status, response = await controller.complete(
                payload,
                _assignment_token(request),
            )
        elif request.path == EXECUTION_PATH:
            _reject_unknown_fields(
                payload,
                _ASSIGNMENT_REQUEST_FIELDS
                | frozenset({"execution_id", "executionId", "workflow"}),
            )
            status, response = await controller.execute(
                payload,
                _assignment_token(request),
            )
        elif request.path == RESULT_PATH:
            _reject_unknown_fields(
                payload,
                _ASSIGNMENT_REQUEST_FIELDS
                | frozenset({"execution_id", "executionId"}),
            )
            status, response = await controller.result(
                payload,
                _assignment_token(request),
            )
        elif request.path == CANCEL_PATH:
            _reject_unknown_fields(
                payload,
                _ASSIGNMENT_REQUEST_FIELDS
                | frozenset({"execution_id", "executionId"}),
            )
            status, response = await controller.cancel(
                payload,
                _assignment_token(request),
            )
        else:
            _reject_unknown_fields(
                payload,
                _ASSIGNMENT_REQUEST_FIELDS | frozenset({"baseline"}),
            )
            status, response = await controller.reset(
                payload,
                _assignment_token(request),
            )
        return _worker_json_response(response, status=status)
    except WorkerRequestTooLarge as error:
        return _worker_json_response({"ok": False, "error": str(error)}, status=413)
    except WorkerProtocolError as error:
        return _worker_json_response({"ok": False, "error": str(error)}, status=400)
    except WorkerProtocolUnavailable as error:
        return _worker_json_response({"ok": False, "error": str(error)}, status=503)
    except Exception:
        # Never serialize exception details or claim a ready worker after an
        # unexpected protocol failure.  Keep ordinary gateway requests
        # isolated from failures in this optional namespace.
        return _worker_json_response(
            {"ok": False, "error": "worker protocol failure"},
            status=500,
        )


async def route(request: web.Request) -> web.StreamResponse:
    if request.path == HEALTH_PATH:
        return web.json_response({"ok": True})

    if request.path == WORKER_PREFIX or request.path.startswith(f"{WORKER_PREFIX}/"):
        return await worker_route(request)

    expected = request.app["pod_token"]
    if not expected or not token_matches(request.headers.get(TOKEN_HEADER), expected):
        return web.json_response({"error": "unauthorized"}, status=401)

    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await proxy_websocket(request)
    return await proxy_http(request)


async def create_client_session(app: web.Application) -> None:
    app["client_session"] = ClientSession(
        timeout=ClientTimeout(total=None, connect=15, sock_connect=15)
    )


async def close_client_session(app: web.Application) -> None:
    await app["client_session"].close()


async def initialize_worker(app: web.Application) -> None:
    controller = app.get("worker_controller")
    if isinstance(controller, WorkerController):
        await controller.initialize()


def supervisor_restart_callback() -> bool:
    """Ask the shell supervisor to restart ComfyUI through a fixed signal.

    The callback is intentionally not a general process launcher: it only
    signals the PID exported by ``start.sh`` and is used after a failed reset
    barrier.  An absent or invalid PID is a hard failure.
    """

    raw_pid = os.environ.get("COMFY_POD_SUPERVISOR_PID", "")
    try:
        pid = int(raw_pid)
    except ValueError:
        return False
    if pid <= 1:
        return False
    try:
        os.kill(pid, signal.SIGUSR1)
    except (OSError, ValueError):
        return False
    return True


def create_app(
    token: str | None = None,
    *,
    worker_capability_secret: str | None = None,
    worker_controller: WorkerController | None = None,
    worker_id: str | None = None,
    worker_initial_state: str = "starting",
    worker_baseline: Mapping[str, object] | None = None,
    barrier_factory: Callable[[], object] | None = None,
    reset_barrier: object | None = None,
    restart_callback: RestartCallback | None = None,
    now_fn: Callable[[], int] = _now_millis,
    baseline_probe: Callable[[], object | Awaitable[object]] | None = None,
    restart_probe: Callable[[], object | Awaitable[object]] | None = None,
    restart_generation_path: str | None = None,
    worker_instance_root: str | None = None,
    comfy_client: ComfyExecutionClient | None = None,
    restart_probe_timeout_seconds: float = 30.0,
) -> web.Application:
    app = web.Application(client_max_size=0)
    capability = (
        configured_worker_capability_secret()
        if worker_capability_secret is None
        else worker_capability_secret
    )
    if token is None:
        app["pod_token"] = (
            os.environ.get("COMFY_POD_TOKEN", "")
            if capability
            else configured_token()
        )
    else:
        app["pod_token"] = token
    app["worker_capability"] = capability
    if capability and worker_controller is None and _WORKER_LIFECYCLE is not None:
        worker_controller = WorkerController(
            worker_id or os.environ.get("COMFY_WORKER_ID", "pod-worker"),
            initial_state=worker_initial_state,
            baseline=worker_baseline,
            barrier_factory=barrier_factory,
            reset_barrier=reset_barrier,
            restart_callback=restart_callback,
            now_fn=now_fn,
            baseline_probe=baseline_probe,
            restart_probe=restart_probe,
            restart_generation_path=restart_generation_path,
            instance_root=worker_instance_root,
            comfy_client=comfy_client,
            restart_probe_timeout_seconds=restart_probe_timeout_seconds,
        )
    app["worker_controller"] = worker_controller
    app.on_startup.append(create_client_session)
    app.on_startup.append(initialize_worker)
    app.on_cleanup.append(close_client_session)
    app.router.add_route("*", "/{path:.*}", route)
    return app


if __name__ == "__main__":
    configured_token_value = os.environ.get("COMFY_POD_TOKEN")
    capability = configured_worker_capability_secret()
    web.run_app(
        create_app(
            token=configured_token_value or "",
            worker_capability_secret=capability,
            restart_callback=supervisor_restart_callback if capability else None,
        ),
        host="0.0.0.0",
        port=int(os.environ.get("COMFY_POD_PORT", "8189")),
        access_log_format='%a "%r" %s %Tf',
    )
