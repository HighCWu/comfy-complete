# Runtime portability allowance review

The checked-in [`ci/runtime-portability-gate.json`](../ci/runtime-portability-gate.json)
contains exact finding fingerprints reviewed from the fixed
`runtime-portability/v2` report. It is intentionally deny-by-default: a new path,
ABI, interpreter, or changed finding does not match an existing allowance.

## Reproduce the policy

The report used for this review is:

`/tmp/runtime-portability-31806909410.GwJNpW/audit-output/runtime-portability.json`

Generate or verify the policy with:

```sh
python scripts/review_runtime_portability_allowances.py \
  --report /tmp/runtime-portability-31806909410.GwJNpW/audit-output/runtime-portability.json \
  --output ci/runtime-portability-gate.json

python scripts/review_runtime_portability_allowances.py \
  --report /tmp/runtime-portability-31806909410.GwJNpW/audit-output/runtime-portability.json \
  --check-policy ci/runtime-portability-gate.json \
  --output /dev/null
```

The generator checks the report schema, critical profile, static-policy profile,
raw blocker count (175), uniqueness of every full finding fingerprint, and the
empty-policy gate evidence. Every group also has an explicit path plus
interpreter/DT_NEEDED mapping and an expected count; any mismatch fails closed.
It also pins the complete report SHA-256, so the command rejects a same-sized
report with changed findings until this review is deliberately refreshed.

## Reviewed allowances

The policy allows 136 of the 175 blockers in seven exact groups:

| Group | Count | Review basis |
| --- | ---: | --- |
| `non-entry-shebangs` | 6 | Vendored `cgi.py` and Qt `licensecheck.pl` helpers; none is `/app/entrypoint.sh`, `/app/comfyui/main.py`, or `/opt/conda/bin/python`. |
| `geometrypack-pyside-gui-extensions` | 32 | Optional GeometryPack PySide6 Qt GUI-extension bindings. |
| `bitsandbytes-non-target-backends` | 31 | CUDA 11.8, CUDA 13, ROCm 6/7, and XPU selector alternatives; target inventory is CUDA 12. |
| `cufile-rdma-plugin` | 9 | Optional cuFile RDMA plugin objects in the two isolated environments and base environment. |
| `rocm-hip-backends` | 2 | Optional HIP/ROCm backend objects; the target profile is NVIDIA. |
| `cupy-optional-cuda-libraries` | 2 | Optional CuPy `cudnn8` and `cutensor2` bindings. |
| `torio-ffmpeg4-5-old-abi` | 54 | FFmpeg4/5 compatibility ABIs across both isolated selector environments and the base environment. |

Every allowance stores the complete finding fingerprint, sorted uniquely, with
`max_matches` equal to its exact fingerprint count. No allowance is keyed only
by a finding code, library name, or path prefix.

## Findings intentionally still blocking

The remaining 39 blockers are deliberately not allowed:

- FFmpeg6 ABI variants: 27
- `libmvec.so.1`: 3
- `libXt.so.6`: 2
- pymeshlab system dependencies: 4
- `libsox.so`: 3

These stay blockers until the enhanced slim launcher inventory is produced and
the main runtime-portability review is repeated. In particular, this policy
does not pre-approve anything that another inventory change may make real.
