"""Read node metadata from an already-running immutable Docker image."""

import argparse
import json
import pathlib
import re
import subprocess
import time


MAX_BYTES = 16 * 1024 * 1024


def validate_identity(image, container, inspected):
    if not re.fullmatch(r"ghcr\.io/[a-z0-9_.-]+/comfy-complete(?:-pod|-pod-slim)?:[a-f0-9]{40}", image):
        raise ValueError("An immutable image reference is required")
    if not re.fullmatch(r"[a-f0-9]{64}", container.get("id", "")) or container.get("running") is not True:
        raise ValueError("A running container is required")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", inspected.get("id", "")) or container.get("image") != inspected["id"]:
        raise ValueError("Container image identity mismatch")
    repository = image.rsplit(":", 1)[0]
    digests = set(value for value in inspected.get("repo_digests", []) if value.startswith(repository + "@sha256:"))
    if len(digests) != 1 or not re.fullmatch(re.escape(repository) + r"@sha256:[a-f0-9]{64}", next(iter(digests))):
        raise ValueError("An unambiguous repository digest is required")


def validate_nodes(value):
    if not isinstance(value, dict) or not 0 < len(value) <= 10000:
        raise ValueError("Non-empty object_info is required")
    for name, node in value.items():
        if not isinstance(name, str) or not 0 < len(name) <= 256 or not isinstance(node, dict):
            raise ValueError("Invalid node identity")
        if not isinstance(node.get("input"), dict) or not isinstance(node.get("output_node"), bool):
            raise ValueError("Invalid node metadata")


def docker(*args):
    result = subprocess.run(["docker", *args], check=True, capture_output=True, timeout=30)
    if len(result.stdout) > MAX_BYTES:
        raise ValueError("Capture exceeds byte limit")
    return json.loads(result.stdout)


def collect(image, container_id, output):
    if not re.fullmatch(r"[a-f0-9]{64}", container_id):
        raise ValueError("A full container ID is required")
    container_format = '{"id":"{{.Id}}","image":"{{.Image}}","running":{{.State.Running}}}'
    image_format = '{"id":"{{.Id}}","repo_digests":{{json .RepoDigests}}}'
    container = docker("container", "inspect", "--format", container_format, container_id)
    inspected = docker("image", "inspect", "--format", image_format, image)
    validate_identity(image, container, inspected)
    if container["id"] != container_id:
        raise ValueError("Selected container changed")
    reader = "\n".join([
        "import sys, urllib.request",
        "opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))",
        "with opener.open('http://127.0.0.1:8188/object_info', timeout=10) as response:",
        f" data = response.read({MAX_BYTES + 1})",
        f"if len(data) > {MAX_BYTES}: raise ValueError('Capture exceeds limit')",
        "sys.stdout.buffer.write(data)",
    ])
    nodes = docker("exec", container_id, "/opt/conda/bin/python", "-B", "-c", reader)
    validate_nodes(nodes)
    if docker("container", "inspect", "--format", container_format, container_id) != container:
        raise ValueError("Container changed during collection")
    if docker("image", "inspect", "--format", image_format, image) != inspected:
        raise ValueError("Image changed during collection")
    output.mkdir(mode=0o700)
    source = {"image": image, "container": container, "local_image": inspected,
              "collected_at": int(time.time() * 1000), "cpu_only": True, "runtime_attested": False}
    for name, value in [("object-info.json", nodes), ("docker-source.json", source)]:
        with (output / name).open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return {"image": image, "class_count": len(nodes), "runtime_attested": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    arguments = parser.parse_args()
    print(json.dumps(collect(arguments.image, arguments.container, arguments.output)))
