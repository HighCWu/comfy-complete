"""Unit tests for the Pod gateway's fail-closed authentication boundary."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pod_gateway", ROOT / "docker" / "pod" / "gateway.py"
)
assert SPEC and SPEC.loader
gateway = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gateway)


class PodGatewayTest(unittest.TestCase):
    def test_token_match_is_exact(self) -> None:
        self.assertTrue(gateway.token_matches("secret", "secret"))
        self.assertFalse(gateway.token_matches("secret-x", "secret"))
        self.assertFalse(gateway.token_matches(None, "secret"))

    def test_configured_token_fails_closed(self) -> None:
        previous = os.environ.pop("COMFY_POD_TOKEN", None)
        try:
            with self.assertRaisesRegex(RuntimeError, "COMFY_POD_TOKEN"):
                gateway.configured_token()
        finally:
            if previous is not None:
                os.environ["COMFY_POD_TOKEN"] = previous

    def test_upstream_defaults_to_loopback(self) -> None:
        old_host = os.environ.pop("COMFY_INTERNAL_HOST", None)
        old_port = os.environ.pop("COMFY_INTERNAL_PORT", None)
        try:
            self.assertEqual(gateway.upstream_base_url(), "http://127.0.0.1:8188")
        finally:
            if old_host is not None:
                os.environ["COMFY_INTERNAL_HOST"] = old_host
            if old_port is not None:
                os.environ["COMFY_INTERNAL_PORT"] = old_port

    def test_gateway_secret_is_not_forwarded(self) -> None:
        class Request:
            headers = {
                "Host": "pod.example",
                "Content-Length": "42",
                "X-Comfy-Pod-Token": "secret",
                "X-Trace": "trace-1",
            }

        self.assertEqual(gateway.forwarded_headers(Request()), {"X-Trace": "trace-1"})


if __name__ == "__main__":
    unittest.main()
