# Runtime object publisher

`scripts/publish_runtime.py` publishes the verified `tar.zst` runtime export to
an S3-compatible object store. It is intentionally provider-neutral and is
safe to keep in a public repository.

The publisher has three object classes:

1. `runtimes/archives/sha256-<archive-sha256>.tar.zst` is the immutable,
   content-addressed archive.
2. `runtimes/manifests/sha256-<runtime-sha256>-<archive-sha256>.json` is the
   immutable manifest, preserving the exact local manifest bytes so the
   `READY.json` manifest hash remains meaningful.
3. `runtimes/channels/<channel>.json` is the small mutable pointer. It is
   written only after the archive and manifest have passed post-upload HEAD
   checks. A single object PUT is the atomic pointer replacement; old objects
   are not deleted by this tool.

The optional key prefix is prepended to all three paths. The channel payload
contains object keys and verified digests, never URLs, credentials, or model
data.

## Configuration

The preferred generic environment variables are:

| Variable | Required | Meaning |
| --- | --- | --- |
| `OBJECT_STORE_ENDPOINT` | yes | HTTPS S3-compatible endpoint |
| `OBJECT_STORE_BUCKET` | yes | bucket name |
| `OBJECT_STORE_ACCESS_KEY_ID` | yes | access key |
| `OBJECT_STORE_SECRET_ACCESS_KEY` | yes | secret key |
| `OBJECT_STORE_REGION` | no | signing region; defaults to `auto` |
| `OBJECT_STORE_PREFIX` | no | safe object-key prefix |

`S3_*` and `R2_*` names are accepted as compatibility aliases. Generic names
take precedence. CLI options take precedence over all environment variables.
Secrets are passed directly to boto3 and are never printed.
The credential needs object read and write access to the selected bucket:
publishing uses HEAD as well as multipart/PUT operations, but does not require
bucket-administration permission. The workflow creates an isolated Python
virtual environment and installs the reviewed `boto3==1.40.0` client only in
the explicitly enabled publication step.

When all four required values are absent, the command exits quickly and
successfully with `status=skipped`, without opening the potentially very large
archive. This allows a public build to include the helper without deployment
credentials. A partial configuration always fails closed. Use `--dry-run` when
you want local archive/manifest validation without a client, and use
`--require-config` in an explicitly enabled release job when absence must be an
error.

## Command

After `export_runtime.py` and `runtime_ready.py` have completed:

```bash
python3 scripts/publish_runtime.py \
  --archive /path/to/sha256-<archive-sha256>.tar.zst \
  --manifest /path/to/manifest.json \
  --channel staging \
  --prefix product-runtimes \
  --part-size-mib 128 \
  --require-config
```

Use `--dry-run` to validate the inputs and print the derived keys without
constructing a client or making a network request. The default multipart part
size is 128 MiB; 256 MiB is also supported. Transfers are sequential and
bounded to one part in memory at a time. The ten-thousand-part object-store
limit is enforced.

Before any large upload, the local archive is re-hashed and compared with the
manifest. A matching immutable object is reused only when both its
`ContentLength` and `sha256` metadata match. A mismatch is an error rather than
an overwrite. Multipart failures call `AbortMultipartUpload`; a completed
upload is re-checked with HEAD. The publisher never uses `CopyObject`, never
deletes old versions, and does not manage lifecycle rules.

## Workflow integration

The explicit publish step belongs in the `publish-runtime-slim` job, after it
has already:

1. pulled the exact audited base image;
2. exported and verified the archive;
3. created and validated `manifest.json` and `READY.json`; and
4. built and inspected the slim launcher image;
5. ran the slim launcher contract smoke test (entrypoint/files, executable
   tools, shell syntax, Python compilation, and launcher import).

The smoke test runs the image read-only with a shell entrypoint,
`--network none`, dropped capabilities, and a bounded `/tmp`; it does not
start the launcher, contact RunPod, or request a GPU.

The materialized-runtime smoke writes its critical-probe evidence to a
runner-owned bind mount. It therefore runs that container as the runner's
`$(id -u):$(id -g)` UID/GID: with `--cap-drop ALL`, container UID 0 cannot
bypass the bind mount's host mode bits. The workflow keeps the source
directory owner-only writable and does not rely on `chmod 0777`, host
`chown`, or a capability exception.

The job pushes the immutable slim launcher image first, then invokes the
publisher, and only then removes the runner-local archive. This means a
publisher failure cannot be mistaken for a successful publication, and the
cleanup boundary is unambiguously after the publication attempt.

The `Docker Build` workflow accepts two manual-dispatch inputs:

| Input | Type | Default | Meaning |
| --- | --- | --- | --- |
| `publish_runtime` | boolean | `false` | The only switch that enables object-store publishing |
| `channel` | string | `staging` | One safe channel path component |

The publisher step runs only when `github.event_name` is
`workflow_dispatch` and `publish_runtime` is explicitly `true`. It validates
`channel` against `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` and rejects `.` and
`..`. Ordinary pushes to `main` skip both the validation and publisher steps, receive no
`OBJECT_STORE_*` secrets, and make no object-store writes.

The enabled step passes these GitHub secrets as environment variables:
`OBJECT_STORE_ENDPOINT`, `OBJECT_STORE_BUCKET`,
`OBJECT_STORE_ACCESS_KEY_ID`, `OBJECT_STORE_SECRET_ACCESS_KEY`, with optional
`OBJECT_STORE_REGION` and `OBJECT_STORE_PREFIX`. It invokes
`scripts/publish_runtime.py --require-config`, so an enabled release fails
closed if the configuration is absent or partial.

The command's JSON output is saved as the small
`runtime-publisher.json` metadata file and copied into the GitHub step
summary. The existing metadata artifact may contain this JSON alongside
`manifest.json`, `READY.json`, `runtime-summary.json`, and `slim-image.json`,
but never the `.tar.zst` archive.

Run the local tests with:

```bash
python3 -m unittest tests/test_runtime_publisher.py -v
```
