# Local runtime Network Volume materializer

`scripts/materialize_runtime.py` expands one already-downloaded runtime
bundle into a local Network Volume. It is deliberately provider-neutral: the
archive and manifest must be present on the local filesystem, and the helper
does not import an object-store SDK, call RunPod, or read credentials.

The materializer owns this layout below the supplied volume root:

```text
<volume-root>/runtimes/
├── .materialize.lock
├── .staging/                       # private, removed after failed attempts
├── <runtime-tree-sha256-hex>/       # immutable published generation
│   ├── <manifest file-tree>
│   ├── manifest.json                # exact input bytes
│   └── READY.json                   # runtime_ready.py contract
└── current -> <runtime-tree-sha256-hex>
```

The generation name is the manifest's `runtime_digest` without the
`sha256:` prefix. Older generations are never deleted or overwritten.
`current` is a relative symlink and is changed only after a complete staged
verification succeeds.

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
the immutable generation, and `current` is atomically replaced last. A
failure before that rename removes only the private staging directory, so an
existing `current` remains untouched. If updating `current` fails after the
generation rename, the new generation is retained for audit/retry and the
previous `current` is not removed by this tool.

If the same runtime digest already exists, the helper does not re-extract it.
It first performs the same full manifest/READY/tree verification and requires
the exact manifest bytes to match; only a verified generation can be reused.

## Output contract

Successful invocations print one compact JSON object containing the status,
runtime/archive digests, archive byte count, entry count, and whether
`current` changed. Errors print one compact JSON object with a bounded error
code and exit with status 2. Input filesystem paths, credentials, URLs, and
member payloads are never included in command output.
