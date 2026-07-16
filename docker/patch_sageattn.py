#!/usr/bin/env python3
"""Patch SageAttention setup.py to build for all supported GPU architectures
without requiring a GPU at build time.

SageAttention's setup.py auto-detects GPUs via torch.cuda.get_device_capability().
On CI runners (no GPU) this raises RuntimeError("No GPUs found").

This script replaces the detection block with a hardcoded list of all
architectures SageAttention supports:
  8.0  - Ampere  (A100)
  8.6  - Ampere  (RTX 30xx, A10, A40)
  8.9  - Ada     (RTX 40xx, L40S, L4)
  9.0  - Hopper  (H100, H200)
  12.0 - Blackwell (RTX 50xx)

Usage: python patch_sageattn.py <path/to/setup.py>
"""
import sys
import pathlib

TARGET_ARCHS = '{"8.0", "8.6", "8.9", "9.0", "12.0"}'

OLD_BLOCK = """compute_capabilities = set()
device_count = torch.cuda.device_count()
for i in range(device_count):
    major, minor = torch.cuda.get_device_capability(i)
    if major < 8:
        warnings.warn(f"skipping GPU {i} with compute capability {major}.{minor}")
        continue
    compute_capabilities.add(f"{major}.{minor}")"""

NEW_LINE = f"compute_capabilities = {TARGET_ARCHS}"


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <path/to/setup.py>", file=sys.stderr)
        sys.exit(1)

    setup_py = pathlib.Path(sys.argv[1])
    content = setup_py.read_text()

    if OLD_BLOCK not in content:
        print(f"ERROR: GPU detection block not found in {setup_py}", file=sys.stderr)
        print("The setup.py format may have changed — update this patch.", file=sys.stderr)
        sys.exit(1)

    setup_py.write_text(content.replace(OLD_BLOCK, NEW_LINE))
    print(f"Patched {setup_py.name}: GPU auto-detect -> hardcoded {TARGET_ARCHS}")


if __name__ == "__main__":
    main()
