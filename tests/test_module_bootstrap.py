"""Generic loader contract; no platform runtime dependencies."""
import hashlib
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("module_bootstrap", Path(__file__).resolve().parents[1] / "docker/module-bootstrap.py")
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)


class LoaderContract(unittest.TestCase):
    def test_manifest(self):
        import json
        manifest = {"version": 1, "entrypoint": "start.sh", "files": {"start.sh": {"size": 0, "sha256": hashlib.sha256(b"").hexdigest()}}}
        self.assertEqual(loader.manifest_from_bytes(json.dumps(manifest).encode()), manifest)

    def test_traversal(self):
        for name in ("../start.sh", "/start.sh", "dir/start.sh", "a..sh"):
            self.assertFalse(loader.valid_name(name))

    def test_invalid_manifest(self):
        for encoded in (b"null", b"{}", b'{"version":1,"entrypoint":"start.sh","files":{}}'):
            with self.assertRaises(loader.ModuleBootstrapError):
                loader.manifest_from_bytes(encoded)

    def test_wrapper_only_embeds_loader(self):
        text = (Path(__file__).resolve().parents[1] / "docker/Dockerfile.pod").read_text()
        self.assertIn("docker/module-bootstrap.py", text)
        self.assertNotIn("COPY docker/pod", text)
        self.assertNotIn("gateway.py", text)


if __name__ == "__main__":
    unittest.main()
