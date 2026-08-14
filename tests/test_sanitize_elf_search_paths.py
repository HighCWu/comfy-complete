"""Tests for the build-time ELF RPATH/RUNPATH sanitizer."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sanitize_elf_search_paths", ROOT / "scripts" / "sanitize_elf_search_paths.py"
)
assert SPEC and SPEC.loader
sanitize = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sanitize
SPEC.loader.exec_module(sanitize)


class SanitizeElfSearchPathsTests(unittest.TestCase):
    def rootfs(self) -> Path:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "opt/conda/bin").mkdir(parents=True)
        (root / "app/comfyui/bin").mkdir(parents=True)
        return root

    def compiler(self) -> str:
        compiler = shutil.which("cc") or shutil.which("gcc")
        if compiler is None:
            self.skipTest("a C compiler is unavailable")
        return compiler

    def tools(self) -> tuple[str, str]:
        readelf = shutil.which("readelf")
        patchelf = shutil.which("patchelf")
        if readelf is None or patchelf is None:
            self.skipTest("readelf and patchelf are required")
        return readelf, patchelf

    def compile_elf(
        self,
        root: Path,
        *,
        name: str,
        search_path: str | None = None,
        old_dtags: bool = False,
        directory: str = "opt/conda/bin",
    ) -> Path:
        source = root / f"{name}.c"
        source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
        destination = root / directory / name
        command = [self.compiler(), str(source)]
        if search_path is not None:
            command.append(f"-Wl,-rpath,{search_path}")
        if old_dtags:
            command.append("-Wl,--disable-new-dtags")
        destination.parent.mkdir(parents=True, exist_ok=True)
        command.extend(("-o", str(destination)))
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        destination.chmod(0o755)
        return destination

    def dynamic(self, path: Path, readelf: str):
        return sanitize.read_dynamic(path, readelf=readelf)

    def fake_tool(self, root: Path, name: str, body: str) -> Path:
        tool = root / name
        tool.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        tool.chmod(0o755)
        return tool

    def test_empty_components_are_removed_for_rpath_and_runpath(self) -> None:
        readelf, patchelf = self.tools()
        cases = (
            ("origin-runpath", "$ORIGIN:", False, "$ORIGIN", "RUNPATH"),
            ("multi-runpath", "/a:/b:", False, "/a:/b", "RUNPATH"),
            (
                "cuda-rpath",
                "/usr/local/cuda-*/lib64::::::::",
                True,
                "/usr/local/cuda-*/lib64",
                "RPATH",
            ),
            ("rocm-rpath", "/opt/rocm/lib:", True, "/opt/rocm/lib", "RPATH"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "opt/conda/bin").mkdir(parents=True)
            (root / "app/comfyui/bin").mkdir(parents=True)
            binaries = {
                name: self.compile_elf(
                    root,
                    name=name,
                    search_path=value,
                    old_dtags=old_dtags,
                )
                for name, value, old_dtags, _cleaned, _tag in cases
            }
            before = {name: self.dynamic(binary, readelf) for name, binary in binaries.items()}
            original_metadata = {name: binary.stat() for name, binary in binaries.items()}
            report = sanitize.sanitize_tree(root, readelf=readelf, patchelf=patchelf)
            self.assertEqual(report.scanned_elf_files, len(cases))
            self.assertEqual(len(report.modified_files), len(cases))
            for name, _value, _old_dtags, cleaned, tag in cases:
                info = self.dynamic(binaries[name], readelf)
                self.assertEqual(info.search_paths, ((tag, cleaned),))
                self.assertFalse(any(sanitize.has_empty_component(value) for _tag, value in info.search_paths))
                self.assertEqual(before[name].entries_without_search_paths(), info.entries_without_search_paths())
                updated = binaries[name].stat()
                original = original_metadata[name]
                self.assertEqual(updated.st_mode, original.st_mode)
                self.assertEqual(updated.st_uid, original.st_uid)
                self.assertEqual(updated.st_gid, original.st_gid)

    def test_non_empty_duplicates_and_order_are_preserved(self) -> None:
        readelf, patchelf = self.tools()
        root = self.rootfs()
        binary = self.compile_elf(root, name="duplicates", search_path="/a:/a::/b:")
        before = self.dynamic(binary, readelf)
        report = sanitize.sanitize_tree(root, readelf=readelf, patchelf=patchelf)
        after = self.dynamic(binary, readelf)
        self.assertEqual(report.modified_files, (str(binary),))
        self.assertEqual(after.search_paths, (("RUNPATH", "/a:/a:/b"),))
        self.assertEqual(before.entries_without_search_paths(), after.entries_without_search_paths())

    def test_clean_elf_is_byte_for_byte_unchanged(self) -> None:
        readelf, patchelf = self.tools()
        root = self.rootfs()
        binary = self.compile_elf(root, name="clean")
        original = binary.read_bytes()
        report = sanitize.sanitize_tree(root, readelf=readelf, patchelf=patchelf)
        self.assertEqual(report.modified_files, ())
        self.assertEqual(binary.read_bytes(), original)

    def test_symlink_to_elf_is_not_scanned_or_modified(self) -> None:
        readelf, patchelf = self.tools()
        root = self.rootfs()
        outside = self.compile_elf(
            root,
            name="outside",
            search_path="$ORIGIN:",
            directory="outside",
        )
        link = root / "opt/conda/bin/link"
        link.symlink_to(outside)
        original = outside.read_bytes()
        report = sanitize.sanitize_tree(root, readelf=readelf, patchelf=patchelf)
        self.assertEqual(report.scanned_elf_files, 0)
        self.assertEqual(report.modified_files, ())
        self.assertEqual(outside.read_bytes(), original)

    def test_patchelf_failure_is_fail_closed_and_restores_file(self) -> None:
        readelf, _patchelf = self.tools()
        root = self.rootfs()
        binary = self.compile_elf(root, name="patch-failure", search_path="$ORIGIN:")
        original = binary.read_bytes()
        fake_patchelf = self.fake_tool(root, "patchelf-fails", "exit 42")
        with self.assertRaises(sanitize.SanitizerError):
            sanitize.sanitize_tree(root, readelf=readelf, patchelf=str(fake_patchelf))
        self.assertEqual(binary.read_bytes(), original)
        self.assertEqual(list(binary.parent.glob(f".{binary.name}.elf-sanitize-*")), [])

    def test_malformed_readelf_is_fail_closed_before_patch(self) -> None:
        _readelf, patchelf = self.tools()
        root = self.rootfs()
        binary = self.compile_elf(root, name="readelf-failure", search_path="$ORIGIN:")
        original = binary.read_bytes()
        fake_readelf = self.fake_tool(root, "readelf-malformed", "echo malformed")
        with self.assertRaises(sanitize.SanitizerError):
            sanitize.sanitize_tree(root, readelf=str(fake_readelf), patchelf=patchelf)
        self.assertEqual(binary.read_bytes(), original)

    def test_missing_scan_target_fails_closed(self) -> None:
        readelf, patchelf = self.tools()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "opt/conda").mkdir(parents=True)
            with self.assertRaises(sanitize.SanitizerError):
                sanitize.sanitize_tree(root, readelf=readelf, patchelf=patchelf)


if __name__ == "__main__":
    unittest.main()
