#!/usr/bin/env python3
"""Types and fail-closed validation for flattened runtime manifests.

The manifest is deliberately independent from Docker, R2, RunPod, and D1.
It describes an immutable *final file tree* export. The canonical file-tree
digest is the runtime identity; archive bytes are independently addressed so
compression-tool changes do not create a new runtime.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import unicodedata
from typing import Any, Literal, TypedDict, cast


SCHEMA_VERSION = 1
RUNTIME_TARGETS = frozenset(("/opt/conda", "/app/comfyui"))
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CONTENT_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
BUILD_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_METADATA_STRING_LENGTH = 512
MAX_PATH_LENGTH = 4096
MAX_DIRECTORY_NAME_LENGTH = 255
MAX_ENTRYPOINT_ARG_LENGTH = 4096
MAX_ENTRYPOINT_ARG_COUNT = 128


class RuntimeManifestError(ValueError):
    """Raised when a runtime manifest is incomplete or unsafe."""


class FileEntry(TypedDict, total=False):
    path: str
    type: Literal["file", "directory", "symlink"]
    mode: int
    size_bytes: int
    sha256: str
    link_target: str


class RuntimeManifest(TypedDict):
    schema_version: int
    runtime_version: str
    runtime_digest: str
    source: dict[str, str]
    compatibility: dict[str, str]
    entrypoint: dict[str, Any]
    targets: list[str]
    selection_policy: dict[str, Any]
    file_tree: dict[str, Any]
    archive: dict[str, Any]


def canonical_json(value: Any) -> bytes:
    """Encode JSON without whitespace or implementation-dependent ordering."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _validate_text(value: object, *, name: str, max_length: int = MAX_METADATA_STRING_LENGTH) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise RuntimeManifestError(f"{name} must be a non-empty string of at most {max_length} characters")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise RuntimeManifestError(f"{name} contains control characters")
    return value


def is_safe_relative_path(value: object, *, allow_empty: bool = False) -> bool:
    """Return whether *value* is a safe POSIX path inside a bundle.

    Backslashes are rejected as well as POSIX traversal components.  This is
    intentional: a bundle may be consumed by tooling on several platforms,
    and accepting Windows separators would make the archive boundary
    ambiguous.
    """

    if not isinstance(value, str) or len(value) > MAX_PATH_LENGTH or "\x00" in value or "\\" in value:
        return False
    if any(unicodedata.category(char).startswith("C") for char in value):
        return False
    if not value:
        return allow_empty
    if value.startswith("/") or value.endswith("/"):
        return False
    parts = value.split("/")
    return all(part not in ("", ".", "..") for part in parts)


def _validate_digest(value: object, *, name: str, content: bool = False) -> str:
    if not isinstance(value, str):
        raise RuntimeManifestError(f"{name} must be a string")
    pattern = CONTENT_DIGEST_RE if content else SHA256_RE
    if not pattern.fullmatch(value):
        expected = "sha256:<64 hex>" if content else "64 lowercase hex"
        raise RuntimeManifestError(f"{name} must be {expected}")
    return value


def _validate_path(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not is_safe_relative_path(value):
        raise RuntimeManifestError(f"{name} is unsafe")
    return value


def validate_exclude_directory_names(value: object, *, name: str = "exclude_directory_names") -> list[str]:
    """Validate basename-only directory exclusions used by export and audit."""

    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise RuntimeManifestError(f"{name} must be a string array")
    if value != sorted(value) or len(set(value)) != len(value):
        raise RuntimeManifestError(f"{name} must be sorted and unique")
    validated: list[str] = []
    for index, item in enumerate(value):
        if (
            not item
            or len(item) > MAX_DIRECTORY_NAME_LENGTH
            or item in (".", "..")
            or "/" in item
            or "\\" in item
            or any(unicodedata.category(char).startswith("C") for char in item)
        ):
            raise RuntimeManifestError(f"{name}[{index}] must be a safe single directory name")
        validated.append(item)
    return validated


def _validate_entries(entries: object) -> tuple[list[FileEntry], int, str]:
    if not isinstance(entries, list) or not entries:
        raise RuntimeManifestError("file_tree.entries must be a non-empty array")

    validated: list[FileEntry] = []
    paths: set[str] = set()
    total_bytes = 0
    for index, raw in enumerate(entries):
        if not isinstance(raw, dict):
            raise RuntimeManifestError(f"file_tree.entries[{index}] must be an object")
        path = _validate_path(raw.get("path"), name=f"file_tree.entries[{index}].path")
        if path in paths:
            raise RuntimeManifestError(f"duplicate file-tree path: {path}")
        paths.add(path)
        kind = raw.get("type")
        if kind not in ("file", "directory", "symlink"):
            raise RuntimeManifestError(f"file-tree entry {path} has an invalid type")
        mode = raw.get("mode")
        if not isinstance(mode, int) or isinstance(mode, bool) or mode < 0 or mode > 0o7777:
            raise RuntimeManifestError(f"file-tree entry {path} has an invalid mode")
        size = raw.get("size_bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise RuntimeManifestError(f"file-tree entry {path} has an invalid size")

        entry: FileEntry = {
            "path": path,
            "type": cast(Literal["file", "directory", "symlink"], kind),
            "mode": mode,
            "size_bytes": size,
        }
        if kind == "file":
            digest = _validate_digest(raw.get("sha256"), name=f"file-tree entry {path}.sha256")
            if size < 0:
                raise RuntimeManifestError(f"file-tree entry {path} has an invalid size")
            entry["sha256"] = digest
            total_bytes += size
            if "link_target" in raw:
                raise RuntimeManifestError(f"file-tree file {path} has link_target")
        elif kind == "directory":
            if size != 0 or "sha256" in raw or "link_target" in raw:
                raise RuntimeManifestError(f"file-tree directory {path} has file metadata")
        else:
            target = raw.get("link_target")
            if not isinstance(target, str) or len(target) > MAX_PATH_LENGTH or not target or "\x00" in target or "\\" in target:
                raise RuntimeManifestError(f"file-tree symlink {path} has an invalid target")
            if any(unicodedata.category(char).startswith("C") for char in target):
                raise RuntimeManifestError(f"file-tree symlink {path} has an invalid target")
            if target.startswith("/"):
                raise RuntimeManifestError(f"file-tree symlink {path} is absolute")
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
            if resolved == ".." or resolved.startswith("../"):
                raise RuntimeManifestError(f"file-tree symlink {path} escapes the bundle")
            if size != 0 or "sha256" in raw:
                raise RuntimeManifestError(f"file-tree symlink {path} has file metadata")
            entry["link_target"] = target

        validated.append(entry)

    if sorted(paths) != [entry["path"] for entry in validated]:
        raise RuntimeManifestError("file-tree entries must be sorted by path")

    tree_digest = sha256_bytes(canonical_json(validated))
    return validated, total_bytes, tree_digest


def _resolve_entrypoint(entries: list[FileEntry], entrypoint_path: str) -> FileEntry:
    """Resolve an entrypoint through selected-tree symlinks to a regular file."""

    by_path = {entry["path"]: entry for entry in entries}
    current = by_path.get(entrypoint_path)
    if current is None:
        raise RuntimeManifestError("entrypoint.path is not present in the file tree")
    seen: set[str] = set()
    while current["type"] == "symlink":
        path = current["path"]
        if path in seen:
            raise RuntimeManifestError(f"entrypoint symlink cycle at {entrypoint_path}")
        seen.add(path)
        target = current.get("link_target")
        if target is None:
            raise RuntimeManifestError(f"entrypoint symlink metadata is incomplete: {path}")
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
        next_entry = by_path.get(resolved)
        if next_entry is None:
            raise RuntimeManifestError(
                f"entrypoint symlink target is not in the file tree: {path} -> {target}"
            )
        current = next_entry
    if current["type"] != "file" or not current["mode"] & 0o111:
        raise RuntimeManifestError("entrypoint must resolve to an executable file")
    return current


def validate_manifest(value: object) -> RuntimeManifest:
    """Validate and return a manifest, rejecting unsafe or inconsistent data."""

    if not isinstance(value, dict):
        raise RuntimeManifestError("runtime manifest must be an object")
    raw = cast(dict[str, Any], value)
    required = {
        "schema_version",
        "runtime_version",
        "runtime_digest",
        "source",
        "compatibility",
        "entrypoint",
        "targets",
        "selection_policy",
        "file_tree",
        "archive",
    }
    if set(raw) != required:
        missing = sorted(required - set(raw))
        extra = sorted(set(raw) - required)
        detail = []
        if missing:
            detail.append(f"missing={missing}")
        if extra:
            detail.append(f"extra={extra}")
        raise RuntimeManifestError("runtime manifest shape is invalid (" + ", ".join(detail) + ")")

    if raw["schema_version"] != SCHEMA_VERSION:
        raise RuntimeManifestError("unsupported runtime manifest schema_version")
    _validate_text(raw["runtime_version"], name="runtime_version")
    runtime_digest = _validate_digest(raw["runtime_digest"], name="runtime_digest", content=True)

    source = raw["source"]
    if not isinstance(source, dict) or set(source) != {"image", "image_digest", "build_sha"}:
        raise RuntimeManifestError("source metadata is invalid")
    _validate_text(source["image"], name="source.image")
    _validate_digest(source["image_digest"], name="source.image_digest", content=True)
    if not isinstance(source["build_sha"], str) or not BUILD_SHA_RE.fullmatch(source["build_sha"]):
        raise RuntimeManifestError("source.build_sha must be a 40-character lowercase git SHA")

    compatibility = raw["compatibility"]
    if not isinstance(compatibility, dict):
        raise RuntimeManifestError("compatibility metadata is invalid")
    compatibility_required = {"platform", "launcher_digest", "launcher_abi"}
    compatibility_allowed = compatibility_required | {"python_version", "cuda_version", "glibc_version"}
    if not compatibility_required.issubset(compatibility) or not set(compatibility).issubset(compatibility_allowed):
        raise RuntimeManifestError("compatibility metadata is incomplete")
    _validate_text(compatibility["platform"], name="compatibility.platform")
    _validate_digest(compatibility["launcher_digest"], name="compatibility.launcher_digest", content=True)
    _validate_text(compatibility["launcher_abi"], name="compatibility.launcher_abi")
    for optional_name in ("python_version", "cuda_version", "glibc_version"):
        if optional_name in compatibility:
            _validate_text(compatibility[optional_name], name=f"compatibility.{optional_name}")

    entrypoint = raw["entrypoint"]
    if not isinstance(entrypoint, dict) or set(entrypoint) != {"path", "argv"}:
        raise RuntimeManifestError("entrypoint metadata is invalid")
    entrypoint_path = _validate_path(entrypoint["path"], name="entrypoint.path")
    argv = entrypoint["argv"]
    if not isinstance(argv, list) or not argv or len(argv) > MAX_ENTRYPOINT_ARG_COUNT:
        raise RuntimeManifestError("entrypoint.argv must be a non-empty bounded string array")
    for index, item in enumerate(argv):
        _validate_text(item, name=f"entrypoint.argv[{index}]", max_length=MAX_ENTRYPOINT_ARG_LENGTH)
    if argv[0] != entrypoint_path:
        raise RuntimeManifestError("entrypoint.argv[0] must equal entrypoint.path")

    targets = raw["targets"]
    if (
        not isinstance(targets, list)
        or not targets
        or any(not isinstance(target, str) or target not in RUNTIME_TARGETS for target in targets)
        or targets != sorted(targets)
        or len(set(targets)) != len(targets)
    ):
        raise RuntimeManifestError("targets must be absolute, traversal-free source paths")

    selection = raw["selection_policy"]
    if not isinstance(selection, dict) or set(selection) != {
        "targets",
        "include_app",
        "excludes",
        "exclude_directory_names",
    }:
        raise RuntimeManifestError("selection_policy metadata is invalid")
    if selection["targets"] != targets:
        raise RuntimeManifestError("selection_policy.targets must equal targets")

    def validate_absolute_policy_paths(value: object, *, name: str) -> list[str]:
        if not isinstance(value, list) or any(not isinstance(path, str) for path in value):
            raise RuntimeManifestError(f"selection_policy.{name} must be a string array")
        if value != sorted(value) or len(set(value)) != len(value):
            raise RuntimeManifestError(f"selection_policy.{name} must be sorted and unique")
        validated_paths: list[str] = []
        for index, path in enumerate(value):
            if not isinstance(path, str) or not path.startswith("/") or not is_safe_relative_path(path[1:]):
                raise RuntimeManifestError(f"selection_policy.{name}[{index}] is unsafe")
            validated_paths.append(path)
        return validated_paths

    policy_targets = validate_absolute_policy_paths(selection["targets"], name="targets")
    if policy_targets != targets:
        raise RuntimeManifestError("selection_policy.targets is inconsistent")
    include_app = validate_absolute_policy_paths(selection["include_app"], name="include_app")
    if any(not path.startswith("/app/") for path in include_app):
        raise RuntimeManifestError("selection_policy.include_app paths must be under /app")
    excludes = validate_absolute_policy_paths(selection["excludes"], name="excludes")
    exclude_directory_names = validate_exclude_directory_names(
        selection["exclude_directory_names"],
        name="selection_policy.exclude_directory_names",
    )

    selected_prefixes = [path[1:] for path in [*targets, *include_app]]
    excluded_prefixes = [path[1:] for path in excludes]
    for path in excludes:
        if not any(path == target or path.startswith(target + "/") for target in targets):
            raise RuntimeManifestError("selection_policy.excludes must be inside a target")

    file_tree = raw["file_tree"]
    if not isinstance(file_tree, dict) or set(file_tree) != {
        "entry_count",
        "total_bytes",
        "tree_sha256",
        "entries",
    }:
        raise RuntimeManifestError("file_tree metadata is invalid")
    entries, total_bytes, tree_digest = _validate_entries(file_tree["entries"])
    if file_tree["entry_count"] != len(entries) or file_tree["total_bytes"] != total_bytes:
        raise RuntimeManifestError("file_tree summary does not match entries")
    if file_tree["tree_sha256"] != tree_digest:
        raise RuntimeManifestError("file_tree.tree_sha256 does not match entries")
    for entry in entries:
        entry_path = entry["path"]
        if not any(entry_path == prefix or entry_path.startswith(prefix + "/") for prefix in selected_prefixes):
            raise RuntimeManifestError(f"file-tree entry is outside selection policy: {entry_path}")
        if any(entry_path == prefix or entry_path.startswith(prefix + "/") for prefix in excluded_prefixes):
            raise RuntimeManifestError(f"file-tree entry is excluded by selection policy: {entry_path}")
        parts = entry_path.split("/")
        for index, part in enumerate(parts):
            if part not in exclude_directory_names:
                continue
            # The policy excludes real directories, not a symlink or regular
            # file whose basename happens to match.  Any descendant, however,
            # would require that component to be a directory and is forbidden.
            if index < len(parts) - 1 or entry["type"] == "directory":
                raise RuntimeManifestError(
                    f"file-tree entry is inside excluded directory: {entry_path}"
                )

    archive = raw["archive"]
    if not isinstance(archive, dict) or set(archive) != {
        "format",
        "object_name",
        "size_bytes",
        "sha256",
    }:
        raise RuntimeManifestError("archive metadata is invalid")
    if archive["format"] != "tar.zst":
        raise RuntimeManifestError("unsupported runtime archive format")
    object_name = archive["object_name"]
    if not isinstance(object_name, str) or not re.fullmatch(r"sha256-[0-9a-f]{64}\.tar\.zst", object_name):
        raise RuntimeManifestError("archive.object_name is invalid")
    archive_digest = f"sha256:{object_name.removesuffix('.tar.zst').removeprefix('sha256-')}"
    if not isinstance(archive["size_bytes"], int) or isinstance(archive["size_bytes"], bool) or archive["size_bytes"] <= 0:
        raise RuntimeManifestError("archive.size_bytes must be positive")
    _validate_digest(archive["sha256"], name="archive.sha256")
    if archive["sha256"] != archive_digest.removeprefix("sha256:"):
        raise RuntimeManifestError("archive.sha256 is not bound to archive.object_name")

    if runtime_digest != f"sha256:{tree_digest}":
        raise RuntimeManifestError("runtime_digest must equal canonical file_tree.tree_sha256")

    _resolve_entrypoint(entries, entrypoint_path)

    return cast(RuntimeManifest, raw)


def load_manifest(path: str) -> RuntimeManifest:
    with open(path, "r", encoding="utf-8") as handle:
        try:
            value = json.load(handle)
        except json.JSONDecodeError as error:
            raise RuntimeManifestError(f"manifest JSON is invalid: {error}") from error
    return validate_manifest(value)
