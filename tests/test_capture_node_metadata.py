import importlib.util
import pathlib
import unittest


spec = importlib.util.spec_from_file_location("capture_node_metadata", pathlib.Path(__file__).parents[1] / "scripts/capture_node_metadata.py")
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


class CaptureNodeMetadataTests(unittest.TestCase):
    def setUp(self):
        self.image = "ghcr.io/example/comfy-complete-pod:" + "a" * 40
        self.container = {"id": "b" * 64, "image": "sha256:" + "c" * 64, "running": True}
        self.inspected = {"id": self.container["image"], "repo_digests": ["ghcr.io/example/comfy-complete-pod@sha256:" + "d" * 64]}

    def test_exact_running_source(self):
        capture.validate_identity(self.image, self.container, self.inspected)
        capture.validate_nodes({"EmptyLatentImage": {"input": {"required": {}}, "output_node": False}})

    def test_rejects_mutable_stopped_and_wrong_images(self):
        variants = [
            ("ghcr.io/example/comfy-complete-pod:latest", self.container, self.inspected),
            (self.image, {**self.container, "running": False}, self.inspected),
            (self.image, {**self.container, "image": "sha256:" + "e" * 64}, self.inspected),
            (self.image, self.container, {**self.inspected, "repo_digests": []}),
            (self.image, self.container, {**self.inspected, "repo_digests": self.inspected["repo_digests"] + ["ghcr.io/example/comfy-complete-pod@sha256:" + "e" * 64]}),
        ]
        for image, container, inspected in variants:
            with self.subTest(image=image, container=container, inspected=inspected), self.assertRaises(ValueError):
                capture.validate_identity(image, container, inspected)

    def test_rejects_empty_and_incomplete_metadata(self):
        for nodes in [{}, [], {"Invalid": {}}, {"Invalid": {"input": {}, "output_node": "false"}}]:
            with self.subTest(nodes=nodes), self.assertRaises(ValueError):
                capture.validate_nodes(nodes)
