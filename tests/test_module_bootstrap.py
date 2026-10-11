"""Generic loader contract; no platform runtime dependencies."""
import hashlib
import importlib.util
import ssl
import tempfile
import urllib.error
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("module_bootstrap", Path(__file__).resolve().parents[1] / "docker/module-bootstrap.py")
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)


class LoaderContract(unittest.TestCase):
    def test_download_diagnostics_never_include_capabilities(self):
        private_url = "https://private.invalid/?token=must-not-leak"
        cases = [
            (urllib.error.HTTPError(private_url, 403, "must-not-leak", {}, None), "http_403"),
            (urllib.error.URLError(ssl.SSLCertVerificationError("must-not-leak")), "tls_verification"),
            (urllib.error.URLError("must-not-leak"), "connection"),
            (TimeoutError("must-not-leak"), "timeout"),
            (OSError("must-not-leak"), "local_io"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            for error, category in cases:
                class Opener:
                    def open(self, *args, **kwargs):
                        raise error
                with self.subTest(category=category), self.assertRaises(loader.ModuleBootstrapError) as caught:
                    loader.download_archive(private_url, "a" * 64, Path(directory) / "bundle", token="must-not-leak", opener=Opener())
                self.assertEqual(str(caught.exception), f"module download failed ({category})")
                self.assertNotIn("must-not-leak", str(caught.exception))

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
