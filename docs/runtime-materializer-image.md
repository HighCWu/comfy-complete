# CPU runtime-materializer image

`docker/Dockerfile.runtime-materializer` builds a one-shot CPU image for
hydrating the managed runtime cache before a product Pod starts.  It is
deliberately separate from the ComfyUI launcher image: the container has
Python, `zstd`, and CA certificates, but no ComfyUI tree, object-store SDK,
provider API client, or credential.

The image downloads exactly two objects over HTTPS and verifies both before
publishing anything:

```text
RUNTIME_ARCHIVE_URL
RUNTIME_MANIFEST_URL
RUNTIME_ARCHIVE_SHA256
RUNTIME_ARCHIVE_SIZE_BYTES
RUNTIME_MANIFEST_SHA256
RUNTIME_MANIFEST_SIZE_BYTES
```

URLs must use HTTPS, have no user-info or fragment, and every redirect must
also use HTTPS.  The downloader sends only a fixed `GET` request with an
identity content-encoding and a non-secret user agent.  It never adds an
authorization header or reads a provider credential.  The expected archive
digest and size must agree with the validated manifest's archive contract.

The archive is streamed once to the CPU container's disposable `/tmp` disk,
then passed to `scripts/materialize_runtime.py`.  The existing materializer
holds its writer lock and verifies the complete tar stream before atomically
publishing a generation and replacing:

```text
/runpod-volume/runtimes/current
```

`RUNTIME_VOLUME_ROOT` can override `/runpod-volume` for local tests.  The
entrypoint refuses to create a missing volume root, so an absent Network
Volume cannot silently consume container-disk space.  A successful invocation
prints one compact JSON record containing only digests, byte counts, runtime
identity, entry count, and publication status.  Failure prints a bounded
error code and exits `2`; temporary downloads are removed and the previous
`current` generation remains under the materializer's atomic-failure
contract.

The current runtime archive is about 15.75GB, so the CPU Pod's container disk
must leave at least that much free space plus a small safety margin.  The
Network Volume needs enough free space for the materializer's private staging
tree and the published generation; it is not used as a download scratch area
by this image.

Example (with the values supplied by the control plane):

```bash
docker run --rm \
  --mount type=bind,source=/path/to/network-volume,destination=/runpod-volume \
  -e RUNTIME_ARCHIVE_URL=https://temporary.example/archive.tar.zst \
  -e RUNTIME_MANIFEST_URL=https://temporary.example/manifest.json \
  -e RUNTIME_ARCHIVE_SHA256=... \
  -e RUNTIME_ARCHIVE_SIZE_BYTES=... \
  -e RUNTIME_MANIFEST_SHA256=... \
  -e RUNTIME_MANIFEST_SIZE_BYTES=... \
  ghcr.io/highcwu/comfy-complete-runtime-materializer:<immutable-tag>
```

The example URLs are placeholders; no URL, token, or provider secret belongs
in the image, repository, or logs.
