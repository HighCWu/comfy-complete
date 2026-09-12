"""Write the fixed shared-model search paths used by Pod pool workers.

Pool workers are reusable platform resources.  They do not have a user or an
instance lease, so they must never call the lease-bound model bootstrap client.
The platform-owned Network Volume is mounted at the fixed path below and its
model files use the canonical ComfyUI ``models/<folder>/<filename>`` layout.

Keep this list in sync with the pinned ComfyUI ``folder_paths.py``.  Adding a
folder here only registers a search path; it does not download, copy, or
publish any model data.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Final


POOL_SHARED_VOLUME_ROOT: Final[Path] = Path("/runpod-volume")

# ComfyUI v0.27.0's canonical model folder names.  Legacy aliases such as
# ``clip`` and ``unet`` are intentionally omitted: ComfyUI maps those names to
# ``text_encoders`` and ``diffusion_models`` respectively, while the shared
# volume stores only canonical catalogue paths.
CANONICAL_MODEL_FOLDERS: Final[tuple[str, ...]] = (
    "audio_encoders",
    "background_removal",
    "checkpoints",
    "classifiers",
    "clip_vision",
    "configs",
    "controlnet",
    "detection",
    "diffusers",
    "diffusion_models",
    "embeddings",
    "frame_interpolation",
    "geometry_estimation",
    "gligen",
    "hypernetworks",
    "latent_upscale_models",
    "loras",
    "model_patches",
    "optical_flow",
    "photomaker",
    "style_models",
    "text_encoders",
    "upscale_models",
    "vae",
    "vae_approx",
)


class PoolModelPathsError(RuntimeError):
    """A fixed pool-worker model-path configuration could not be written."""


def pool_model_paths_config() -> dict[str, dict[str, str]]:
    """Return the exact extra-path config consumed by ComfyUI.

    The volume root is deliberately not configurable through the environment
    or command line.  Pool worker Pods receive a platform-owned mount at this
    path, and accepting another root would make it possible to register an
    unintended container path as a model source.
    """

    paths: dict[str, str] = {"base_path": str(POOL_SHARED_VOLUME_ROOT)}
    paths.update(
        {
            folder: f"models/{folder}/"
            for folder in CANONICAL_MODEL_FOLDERS
        }
    )
    return {"comfy_pool_shared_models": paths}


def write_pool_extra_model_paths(
    config_path: Path,
    *,
    mounted_volume_root: Path = POOL_SHARED_VOLUME_ROOT,
) -> None:
    """Atomically write the fixed pool-worker config to ``config_path``.

    ``config_path`` is supplied by the trusted startup script and must be
    absolute so an accidental working-directory change cannot redirect the
    config into the runtime or Network Volume.  The temporary file is created
    exclusively and the final replacement is atomic.
    """

    if not config_path.is_absolute():
        raise PoolModelPathsError("pool model-path config must be absolute")
    if not mounted_volume_root.is_dir():
        raise PoolModelPathsError("pool shared Network Volume is not mounted")

    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_name(config_path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        file_descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                pool_model_paths_config(),
                handle,
                ensure_ascii=True,
                sort_keys=True,
            )
            handle.write("\n")
        os.replace(temporary, config_path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise PoolModelPathsError(
            f"could not write pool model-path config: {error}"
        ) from error


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    write_pool_extra_model_paths(args.config)
    print(
        "comfy-pod: fixed pool-worker model paths written — "
        + str(args.config),
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except PoolModelPathsError as error:
        print(f"comfy-pod: pool model paths failed — {error}", flush=True)
        raise SystemExit(1) from error
