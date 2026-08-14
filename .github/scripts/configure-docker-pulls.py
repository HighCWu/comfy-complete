#!/usr/bin/env python3
"""Merge conservative image-pull settings into Docker's daemon config."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


def main() -> None:
    config_path = Path(sys.argv[1] if len(sys.argv) > 1 else "/etc/docker/daemon.json")
    if config_path.exists() and config_path.stat().st_size:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise SystemExit(f"Docker daemon config must be a JSON object: {config_path}")
    else:
        config = {}

    config["max-concurrent-downloads"] = 1
    config["max-download-attempts"] = 5
    config_path.parent.mkdir(parents=True, exist_ok=True)

    descriptor, temporary_name = tempfile.mkstemp(
        dir=config_path.parent,
        prefix=f".{config_path.name}.",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary_name, config_path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    main()
