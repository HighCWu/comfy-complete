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

The generation root and any parent directories synthesized for manifest paths
are published with mode `0755`, so a Pod running under an arbitrary UID can
traverse the verified runtime while group/other users cannot write those
directories. `manifest.json` and `READY.json` are published with mode `0644`.
Modes of files, symlinks, and directories present in the runtime manifest are
never broadened or rewritten. The provider-supplied volume root is not chmod'd;
it must already grant the runtime UID read and traverse access (a permissive
`0777` mount is accepted).

Product Pods mount the trusted runtime cache read-write. The curated ComfyUI
and custom-node bundle may create ordinary interpreter caches or other benign
runtime-local files. User inputs, outputs, temporary assets, and user settings
still use the disposable per-instance tree; the writable runtime cache is not
used as user-data storage or as a tenant isolation boundary.

This is only the offline local `archive -> mounted volume` materialization
step. It is not an R2 downloader, a control-plane worker, or a RunPod
hydration implementation; a separate caller is responsible for obtaining the
already-verified local archive and manifest.

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
links, special files, unsafe symlinks, and ownership/timestamp/mode/size/link
metadata mismatches. Regular-file bytes are streamed into staging while their
size and SHA-256 are recomputed. After extraction, the entire staged tree is
walked again to detect corruption or unmanifested files.

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
runtime/archive digests, archive byte count, entry count, and whether
`current` changed. Errors print one compact JSON object with a bounded error
code and exit with status 2. Input filesystem paths, credentials, URLs, and
member payloads are never included in command output.
