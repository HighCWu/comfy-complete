#!/usr/bin/env python3
"""
Read or update the ComfyUI pin refs in version_lock.yaml + requirements.txt.

Targeted regex edits are used (rather than yq) so header comments and
inter-block blank lines in version_lock.yaml are preserved.

This script lives in the comfy-complete repo (post-migration). It was
previously at cloud/scripts/comfyui_pins.py and operated on cloud's
comfy-complete/ subdir; that subdir is gone and this is the authoritative copy.

Usage:
    comfyui_pins.py read
        Prints the current pins, one per line:
            tag=<comfyui ref>
            frontend=<comfyui_frontend_package ref>
            templates=<comfyui_workflow_templates ref>
        The format is suitable for appending directly to $GITHUB_OUTPUT.

    comfyui_pins.py bump <new_tag> <new_frontend> <new_templates>
        Rewrites the three refs in version_lock.yaml and the two pinned
        lines in requirements.txt. Fails loudly if any pin is missing or
        appears more than once.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_LOCK = REPO_ROOT / "version_lock.yaml"
REQUIREMENTS = REPO_ROOT / "requirements.txt"


def _ref_pattern(key: str) -> str:
    return rf'^  {re.escape(key)}:\n    ref:\s*(\S+)'


def read_pins() -> dict[str, str]:
    text = VERSION_LOCK.read_text()

    def get(key: str) -> str:
        m = re.search(_ref_pattern(key), text, re.M)
        if not m:
            raise SystemExit(f"::error::Could not find pin for {key} in {VERSION_LOCK}")
        return m.group(1)

    return {
        "tag": get("comfyui"),
        "frontend": get("comfyui_frontend_package"),
        "templates": get("comfyui_workflow_templates"),
    }


def bump_pins(new_tag: str, new_frontend: str, new_templates: str) -> None:
    text = VERSION_LOCK.read_text()

    # Lambda replacements so backslashes or '\g' in new values cannot be
    # interpreted as regex backreferences.
    def replace_ref(text: str, key: str, new_value: str) -> str:
        pattern = rf'(^  {re.escape(key)}:\n    ref:\s*)\S+'
        new_text, n = re.subn(pattern, lambda m: m.group(1) + new_value, text, flags=re.M)
        if n != 1:
            raise SystemExit(f"::error::Expected exactly 1 ref for {key} in {VERSION_LOCK}, got {n}")
        return new_text

    text = replace_ref(text, "comfyui", new_tag)
    text = replace_ref(text, "comfyui_frontend_package", new_frontend)
    text = replace_ref(text, "comfyui_workflow_templates", new_templates)
    VERSION_LOCK.write_text(text)

    req = REQUIREMENTS.read_text()
    req, nf = re.subn(
        r'^comfyui_frontend_package==\S+',
        lambda m: f'comfyui_frontend_package=={new_frontend}',
        req,
        flags=re.M,
    )
    req, nt = re.subn(
        r'^comfyui_workflow_templates==\S+',
        lambda m: f'comfyui_workflow_templates=={new_templates}',
        req,
        flags=re.M,
    )
    if nf != 1 or nt != 1:
        raise SystemExit(
            f"::error::Expected exactly 1 pin each in {REQUIREMENTS} (frontend={nf}, templates={nt})"
        )
    REQUIREMENTS.write_text(req)


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)

    cmd = sys.argv[1]
    if cmd == "read":
        if len(sys.argv) != 2:
            print("Usage: comfyui_pins.py read", file=sys.stderr)
            sys.exit(2)
        pins = read_pins()
        for key, value in pins.items():
            print(f"{key}={value}")
    elif cmd == "bump":
        if len(sys.argv) != 5:
            print(
                "Usage: comfyui_pins.py bump <new_tag> <new_frontend> <new_templates>",
                file=sys.stderr,
            )
            sys.exit(2)
        bump_pins(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        print(f"Unknown command: {cmd!r}", file=sys.stderr)
        print(__doc__, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
