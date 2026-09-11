# Local runtime Network Volume materializer

`scripts/materialize_runtime.py` expands one already-downloaded runtime
bundle into a local Network Volume. It is deliberately provider-neutral: the
archive and manifest must be present on the local filesystem, and the helper
does not import an object-store SDK, call RunPod, or read credentials.

The materializer owns this layout below the supplied volume root:

```text
<volume-root>/runtimes/
├── .materialize.lock
├── .staging/                       # private (0700), removed after failed attempts
├── <runtime-tree-sha256-hex>/       # verified base generation (0755)
│   ├── <manifest file-tree>
│   ├── manifest.json                # exact input bytes (0644)
│   └── READY.json                   # runtime_ready.py contract (0644)
└── current -> <runtime-tree-sha256-hex>
```

The generation name is the manifest's `runtime_digest` without the
`sha256:` prefix. Older generations are never deleted or overwritten.
`current` is a relative symlink and is changed only after a complete staged
verification succeeds.

Before extraction, the caller runs a bounded capability probe on the mounted
volume. The probe covers the materializer's control modes and all
distinct file/directory modes present in the manifest, plus symlink type and
mode. It returns one immutable `VolumeModePolicy`, which is passed unchanged
through archive extraction, metadata checks, generation reuse, and publication
checks. If the probe cannot establish a supported behavior, the archive is not
downloaded.

Regular-file modes are either exact or, when the provider consistently
normalizes them, mapped by execution class: non-executable files may become
`0666`, while files with any execute bit may become `0777`. Directory modes
are either exact or mapped to the observed `0777` result. Special permission
bits are always rejected; a file cannot cross execution classes (for example,
`0644` must not become `0777`, and `0755` must not become `0666`). Symlinks
must remain symlinks with the POSIX `0777` `lstat` mode. These mappings are
not a general permission mismatch bypass: types, sizes, bytes, hashes, link
targets, and archive metadata remain strict. The materializer and Pod launcher
share the same bounded mode-matching function so their acceptance rules cannot
drift.

The generation root, `.staging`, and parent directories synthesized for
manifest paths use the same policy: they request `0755` or `0700` and verify
the policy-observed result. `manifest.json` and `READY.json` request `0644`,
and the writer lock requests `0600`; on a provider with the observed file
normalization they are verified as `0666` because they are non-executable.
The provider-supplied volume root is not chmod'd; it must already grant the
runtime UID read and traverse access (a permissive `0777` mount is accepted).

Product Pods mount the trusted runtime cache read-write. The curated ComfyUI
and custom-node bundle may create ordinary interpreter caches or other benign
runtime-local files. User inputs, outputs, temporary assets, and user settings
still use the disposable per-instance tree; the writable runtime cache is not
used as user-data storage or as a tenant isolation boundary.

This is only the offline local `archive -> mounted volume` materialization
step. It is not an R2 downloader, a control-plane worker, or a RunPod
hydration implementation; the control plane stages the archive and manifest
through the Network Volume S3 interface before starting the utility Pod.

The public `download_materialize_runtime.py` entrypoint consumes that staged
pair at the fixed content-addressed locations below and performs a second,
exact size/SHA-256 verification from the mounted files:

```text
<volume-root>/.runtime-incoming/archives/sha256-<archive-sha256>.tar.zst
<volume-root>/.runtime-incoming/manifests/sha256-<manifest-sha256>.json
```

Those input files are caller-owned.  The utility does not remove, rename, or
copy them into container disk; cleanup belongs to the control plane after the
Pod is gone.  The entrypoint's pre-staged mode rejects relative paths,
traversal components, symlinked path components, paths outside the fixed
layout, missing files, and non-regular files before materialization.

The utility wrapper passes the exact manifest bytes it verified into
`materialize_runtime(verified_manifest_bytes=...)`.  The materializer does not
re-read the staged manifest path after that point; those bytes determine the
runtime identity and are the bytes written to the published generation's
`manifest.json`.  Archive size/SHA-256 verification is performed once by the
materializer immediately before extraction, with bounded phase callbacks for
`archive_verify`, `extraction`, and `tree_verify`.

## Command

```bash
python3 scripts/materialize_runtime.py \
  --archive /path/to/sha256-<archive-sha256>.tar.zst \
  --manifest /path/to/manifest.json \
  --volume-root /path/to/network-volume
```

The archive is hashed and size-checked before decompression. Decompression is
performed by the local `zstd` CLI in streaming mode; the tar stream is never
loaded into memory as one object. A blocking `flock` protects the complete
publication sequence, including generation selection and `current` update.

Every tar member must map exactly once to one manifest entry. The materializer
rejects traversal and unsafe names, duplicate/extra/missing members, hard
links in the archive, special files, unsafe symlinks, and ownership/timestamp/
mode/size/link metadata mismatches. Regular-file bytes are streamed into
staging while their size and SHA-256 are recomputed. When two manifest files
have the same SHA-256, size, and mode, the first path is materialized normally;
later aliases still consume and hash their complete tar payload, then become
hardlinks to that already-verified file. This lowers block and inode pressure
without trusting archive payloads or modes. The runtime cache is a curated
trusted tree: code that intentionally rewrites a runtime file must use atomic
replacement semantics, because in-place writes to a deduplicated alias also
affect its identical aliases. After extraction, the entire staged tree is
walked again to detect corruption or unmanifested files.

Regular-file contents are flushed with one filesystem-scoped `syncfs` barrier
after that verification, rather than one `fsync` syscall per archive member.
This keeps the durability boundary before publication while avoiding
per-file syscall latency for the runtime's very large file count. The staged
tree and its entries are still flushed before publication, while the
generation root, metadata directories, and `current` replacement are covered
by the publication-directory fsyncs, a filesystem barrier after the
generation rename and seal but before `current` becomes visible, and a final
filesystem barrier after the `current` replacement.
Network filesystems that explicitly reject directory `fsync` with
`EINVAL`/`ENOTSUP`/`EOPNOTSUPP` use those filesystem barriers instead; all
other directory-sync errors remain terminal. If `syncfs` itself reports one
of those unsupported-operation errors, Linux's system-wide `sync` is the
bounded compatibility fallback.

Only then are the exact manifest bytes and a separately fsynced `READY.json`
written atomically. The completed staging directory is atomically renamed to
the immutable generation. The materializer then seals only its own generation
metadata directories before replacing `current` last. A
failure before that rename removes only the private staging directory, so an
existing `current` remains untouched. If updating `current` fails after the
generation rename, the new generation is retained for audit/retry and the
previous `current` is not removed by this tool.

If the process stops after the generation rename but before metadata sealing,
the next invocation fully verifies that generation, repairs its materializer
owned directory modes, and only then exposes it through `current`.

If the same runtime digest already exists, the helper does not re-extract it.
It requires the exact manifest and READY bytes and re-verifies every
manifest-owned entry. Additional runtime-created files such as `__pycache__`
are tolerated on this reuse path because product Pods use the curated shared
runtime as a trusted writable cache. Initial extraction remains strict and
rejects every unmanifested archive or staging entry.

## Output contract

Successful invocations print one compact JSON object containing the status,
runtime/archive digests, archive byte count, verified materialized file-byte
count, `verified_archive_bytes`, entry count, whether `current` changed, and
best-effort filesystem capacity snapshots (`volume_*_bytes` and `volume_*_inodes`), plus the number
of verified hardlink aliases and logical bytes saved by links. Errors print one
compact JSON object with a bounded error code and, when available, the same
provider-safe capacity counters plus the expected runtime byte/entry totals;
the command exits with status 2. Input filesystem paths, credentials, URLs,
and member payloads are never included in command output. The counters are
diagnostic snapshots, not reservations: a network filesystem may update them
asynchronously, and `f_bavail`/`f_favail` intentionally report capacity
available to the unprivileged materializer process.

Storage failures use distinct bounded codes. A temporary file needed by the
archive decompressor failing because the local scratch filesystem is full is
reported as `archive_temporary_disk_exhausted`; mounted-volume quota or space
exhaustion uses `volume_capacity_exhausted`, filesystem-wide synchronization
uses `volume_sync_failed`, unsupported non-capacity directory synchronization
uses `volume_directory_sync_failed`, and other writes or publication
operations use `volume_write_failed`. Malformed or truncated decompressor
output remains `archive_stream_invalid`. These codes carry no path or
operating-system error text and are safe to pass through the materializer
result callback.
