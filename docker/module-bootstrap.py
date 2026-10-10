"""Generic, bounded bootstrap for a pinned private script module bundle.

Contains no gateway, model-cache or user-task logic. May be shipped in the
public base wrapper; the verified payload is distributed separately.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
import urllib.parse
import urllib.request

MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_FILES = 64
MAX_MANIFEST_BYTES = 16 * 1024
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}\Z")
SHA = re.compile(r"[a-f0-9]{64}\Z")


class ModuleBootstrapError(RuntimeError):
    pass


def valid_name(value):
    return isinstance(value, str) and NAME.fullmatch(value) is not None and ".." not in value


def manifest_from_bytes(encoded: bytes):
    try:
        manifest = json.loads(encoded)
    except (ValueError, UnicodeError):
        raise ModuleBootstrapError("invalid module manifest") from None
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ModuleBootstrapError("invalid module manifest")
    files = manifest.get("files")
    entrypoint = manifest.get("entrypoint")
    if (not isinstance(files, dict) or not 1 <= len(files) <= MAX_FILES
            or not valid_name(entrypoint) or not entrypoint.endswith(".sh") or entrypoint not in files):
        raise ModuleBootstrapError("invalid module manifest")
    names, total = set(), 0
    for name, metadata in files.items():
        if (not valid_name(name) or name == "module.json" or name.casefold() in names
                or not isinstance(metadata, dict) or type(metadata.get("size")) is not int
                or not 0 <= metadata["size"] <= MAX_EXPANDED_BYTES
                or not isinstance(metadata.get("sha256"), str) or not SHA.fullmatch(metadata["sha256"])):
            raise ModuleBootstrapError("invalid module file metadata")
        names.add(name.casefold())
        total += metadata["size"]
    if total > MAX_EXPANDED_BYTES:
        raise ModuleBootstrapError("module exceeds expanded size limit")
    return manifest


def download_archive(url: str, digest: str, destination: Path, *, token: str = "", opener=None):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.fragment or len(url) > 8192 or not SHA.fullmatch(digest)
            or len(token) > 512 or any(ord(char) < 32 for char in token)):
        raise ModuleBootstrapError("invalid module download configuration")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args):
            return None
    opener = opener or urllib.request.build_opener(NoRedirect())
    request = urllib.request.Request(url, headers={"Accept-Encoding": "identity",
        **({"Authorization": "Bearer " + token} if token else {})})
    try:
        with opener.open(request, timeout=30) as response:
            length = response.headers.get("Content-Length", "")
            if (response.status != 200 or response.geturl() != url or not length.isdecimal()
                    or not 0 < int(length) <= MAX_ARCHIVE_BYTES
                    or response.headers.get("Content-Encoding", "").lower() not in ("", "identity")):
                raise ModuleBootstrapError("invalid module response")
            expected, received, checksum = int(length), 0, hashlib.sha256()
            with destination.open("xb") as output:
                while True:
                    chunk = response.read(min(128 * 1024, expected - received + 1))
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > expected:
                        raise ModuleBootstrapError("module download size mismatch")
                    checksum.update(chunk)
                    output.write(chunk)
            if received != expected or checksum.hexdigest() != digest:
                raise ModuleBootstrapError("module download integrity mismatch")
    except Exception:
        destination.unlink(missing_ok=True)
        # Presigned URLs and capability tokens must never enter traceback text.
        raise ModuleBootstrapError("module download failed") from None


def install_archive(archive_path: Path, digest: str, root: Path) -> Path:
    if not SHA.fullmatch(digest) or archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ModuleBootstrapError("invalid module archive")
    checksum = hashlib.sha256()
    with archive_path.open("rb") as source:
        while chunk := source.read(128 * 1024):
            checksum.update(chunk)
    if checksum.hexdigest() != digest:
        raise ModuleBootstrapError("module archive integrity mismatch")
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise ModuleBootstrapError("unsafe module installation root")
    staging = Path(tempfile.mkdtemp(prefix=".install-", dir=root))
    final = root / (digest + "-" + staging.name.removeprefix(".install-"))
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            first = archive.next()
            if not first or first.name != "module.json" or not first.isfile() or not 0 < first.size <= MAX_MANIFEST_BYTES:
                raise ModuleBootstrapError("module manifest must be first")
            manifest_stream = archive.extractfile(first)
            if manifest_stream is None:
                raise ModuleBootstrapError("module manifest missing")
            with manifest_stream:
                manifest = manifest_from_bytes(manifest_stream.read(MAX_MANIFEST_BYTES + 1))
            seen = set()
            for member in iter(archive.next, None):
                metadata = manifest["files"].get(member.name)
                if not member.isfile() or metadata is None or member.name in seen or member.size != metadata["size"]:
                    raise ModuleBootstrapError("unexpected module archive member")
                seen.add(member.name)
                content = archive.extractfile(member)
                if content is None:
                    raise ModuleBootstrapError("module file missing")
                target, checksum, received = staging / member.name, hashlib.sha256(), 0
                with content, target.open("xb") as output:
                    while chunk := content.read(128 * 1024):
                        received += len(chunk)
                        if received > metadata["size"]:
                            raise ModuleBootstrapError("module file exceeds declared size")
                        checksum.update(chunk)
                        output.write(chunk)
                if received != metadata["size"] or checksum.hexdigest() != metadata["sha256"]:
                    raise ModuleBootstrapError("module file integrity mismatch")
                target.chmod(0o755 if member.name == manifest["entrypoint"] else 0o644)
            if seen != set(manifest["files"]):
                raise ModuleBootstrapError("module archive is incomplete")
        staging.rename(final)
        return final / manifest["entrypoint"]
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise ModuleBootstrapError("module installation failed") from None


def main():
    url = os.environ.get("POD_MODULE_BUNDLE_URL", "")
    digest = os.environ.get("POD_MODULE_BUNDLE_SHA256", "")
    token = os.environ.get("POD_MODULE_BUNDLE_TOKEN", "")
    if not url or not token or not SHA.fullmatch(digest):
        raise ModuleBootstrapError("pinned module configuration is required")
    with tempfile.TemporaryDirectory(prefix="pod-module-download-") as temporary:
        archive_path = Path(temporary) / "module.tar.gz"
        download_archive(url, digest, archive_path, token=token)
        entrypoint = install_archive(archive_path, digest, Path("/opt/pod-modules"))
    environment = dict(os.environ)
    environment.pop("POD_MODULE_BUNDLE_URL", None)
    environment.pop("POD_MODULE_BUNDLE_TOKEN", None)
    os.execve("/bin/bash", ["bash", str(entrypoint)], environment)


if __name__ == "__main__":
    try:
        main()
    except ModuleBootstrapError as error:
        print(str(error), flush=True)
        raise SystemExit(1) from None
