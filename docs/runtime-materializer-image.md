# CPU runtime-materializer image

`docker/Dockerfile.runtime-materializer` builds a one-shot utility image for
materializing the managed runtime cache before a product Pod starts.  It is
deliberately separate from the ComfyUI launcher image: the container has
Python, `zstd`, and CA certificates, but no ComfyUI tree, object-store SDK,
provider API client, or credential.

The product flow copies the two immutable objects to the mounted Network
Volume through its S3 interface *before* this image is started.  The image
then verifies those mounted files and expands the archive; no GPU/container
Pod is used for the transfer phase.  The pre-staged contract is:

```text
RUNTIME_VOLUME_ROOT=/runpod-volume
RUNTIME_ARCHIVE_PATH=/runpod-volume/.runtime-incoming/archives/sha256-<archive-sha256>.tar.zst
RUNTIME_MANIFEST_PATH=/runpod-volume/.runtime-incoming/manifests/sha256-<manifest-sha256>.json
RUNTIME_ARCHIVE_SHA256
RUNTIME_ARCHIVE_SIZE_BYTES
RUNTIME_MANIFEST_SHA256
RUNTIME_MANIFEST_SIZE_BYTES
RUNTIME_RESULT_URL                 # optional HTTPS capability callback
```

Both staged paths must be absolute, content-addressed, exactly under
`RUNTIME_VOLUME_ROOT`, and free of `.`/`..` components and symlinked path
components.  The entrypoint rechecks the mounted paths immediately before
opening them and verifies the manifest's exact size/SHA-256; the materializer
performs the one authoritative archive size/SHA-256 pass immediately before
extraction.  It rejects a missing mount, a non-regular file, a symlink escape,
a manifest/archive identity mismatch, or a staged path outside the fixed
`.runtime-incoming/{archives,manifests}` layout.  It never deletes or moves
the staged inputs; cleanup is owned by the control plane after the Pod exits.

The old URL-based compatibility mode accepts the following additional
variables and is retained only for old images and tests:

```text
RUNTIME_ARCHIVE_URL
RUNTIME_MANIFEST_URL
RUNTIME_ARCHIVE_SHA256
RUNTIME_ARCHIVE_SIZE_BYTES
RUNTIME_MANIFEST_SHA256
RUNTIME_MANIFEST_SIZE_BYTES
RUNTIME_RESULT_URL                 # optional HTTPS capability callback
```

If either staged path is present, both are required and the URL downloader is
never selected.  A partially configured staged pair fails closed instead of
falling back to a network download.

URLs must use HTTPS, have no user-info or fragment, and every redirect must
also use HTTPS.  The manifest uses one fixed `GET`; the large archive uses
contiguous `Range: bytes=start-end` requests with identity content-encoding
and a non-secret user agent.  A response that ignores Range is rejected, and
each chunk must match its requested `Content-Range`, `Content-Length`, and
body size before the next chunk is requested.  The downloader never adds an
authorization header or reads a provider credential.  The expected archive
digest and size must agree with the validated manifest's archive contract.

When `RUNTIME_RESULT_URL` is present, the entrypoint POSTs one bounded JSON
record after the materialization attempt.  Success contains `ok: true` plus
all scalar fields returned by `run()` (`status`, `runtime_digest`, archive and
manifest digests/sizes, `verified_archive_bytes`, `materialized_bytes`, entry
count, `current_updated`, hardlink count/bytes saved, and best-effort
block/inode capacity snapshots).  The legacy URL branch additionally includes
`downloaded_bytes` for compatibility; the pre-staged product branch never
claims that the GPU utility Pod downloaded bytes.  Failure
contains the bounded shape `{"ok":false,"error_code":"..."}` and may include
provider-safe `diagnostics` counters for the same block/inode snapshot and
expected runtime totals.  A successful
materialization whose result POST fails exits `2` and does not print a success
record, so a callback failure cannot be treated as successful preparation.  A
materialization failure always exits `2`, regardless of whether its failure
callback succeeds.

Phase and progress records are emitted as compact JSON on stderr, so stdout
remains a one-record result channel.  Records include volume validation,
manifest verification, archive verification start/end, bounded
`archive_verify` progress counters, and materialization start/end.  They never
contain URLs, filesystem paths, response bodies, or secrets.

In the product flow the archive stays on the mounted Network Volume.  The
entrypoint verifies the manifest bytes and passes that exact byte string to
`scripts/materialize_runtime.py`; the materializer performs the single
authoritative archive size/SHA-256 pass immediately before zstd extraction.
The existing materializer then holds its writer lock and verifies the complete
tar stream before atomically publishing a generation and replacing. Identical regular files (matching
SHA-256, byte size, and mode) are still fully consumed and verified from the
tar stream, then published as hardlink aliases to reduce Network Volume block
and inode pressure. The curated runtime must treat these source files as
immutable and use atomic replacement for intentional rewrites. The published
generation replaces:

```text
/runpod-volume/runtimes/current
```

`RUNTIME_VOLUME_ROOT` can override `/runpod-volume` for local tests.  The
entrypoint refuses to create a missing volume root, so an absent Network
Volume cannot silently consume container-disk space.  A successful invocation
prints one compact JSON record containing only digests, verified byte counts,
runtime identity, entry count, publication status, and provider-safe capacity
snapshots.  Failure prints a bounded error code and optional capacity
diagnostics, then exits `2`; pre-staged inputs remain untouched and the
previous `current` generation remains under the materializer's atomic-failure
contract.

The Network Volume needs enough free space for the materializer's private
staging tree and the published generation.  The product flow does not require
the utility Pod's disposable container disk to hold a second archive copy.

Example (with the values supplied by the control plane):

```bash
docker run --rm \
  --mount type=bind,source=/path/to/network-volume,destination=/runpod-volume \
  -e RUNTIME_ARCHIVE_PATH=/runpod-volume/.runtime-incoming/archives/sha256-<archive-sha256>.tar.zst \
  -e RUNTIME_MANIFEST_PATH=/runpod-volume/.runtime-incoming/manifests/sha256-<manifest-sha256>.json \
  -e RUNTIME_ARCHIVE_SHA256=... \
  -e RUNTIME_ARCHIVE_SIZE_BYTES=... \
  -e RUNTIME_MANIFEST_SHA256=... \
  -e RUNTIME_MANIFEST_SIZE_BYTES=... \
  -e RUNTIME_RESULT_URL=https://temporary.example/result?signature=short-lived \
  ghcr.io/highcwu/comfy-complete-runtime-materializer:<immutable-tag>
```

The path placeholders are content-addressed values supplied by the control
plane.  No URL, token, or provider secret belongs in the image, repository, or
logs.  The result callback receives only the bounded JSON record and no
authorization header is sent.
