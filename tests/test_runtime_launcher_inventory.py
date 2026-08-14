from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "runtime_launcher_inventory",
    ROOT / "scripts" / "runtime_launcher_inventory.py",
)
assert SPEC and SPEC.loader
inventory = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = inventory
SPEC.loader.exec_module(inventory)


class RuntimeLauncherInventoryTests(unittest.TestCase):
    def rootfs(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for path in (
            "bin/bash",
            "usr/bin/dash",
            "usr/bin/dumb-init",
            "usr/bin/env",
            "usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2",
            "lib/x86_64-linux-gnu/libc.so.6",
        ):
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x")
            target.chmod(0o755)
        (root / "bin/sh").symlink_to("../usr/bin/dash")
        (root / "lib64").mkdir()
        (root / "lib64/ld-linux-x86-64.so.2").symlink_to(
            "../usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"
        )
        return root

    def test_generation_is_sorted_and_normalises_relative_symlinks(self) -> None:
        root = self.rootfs()
        result = inventory.build_inventory(
            root,
            ldconfig_names=("libc.so.6", "libz.so.1"),
        )
        self.assertEqual(result["libraries"], ["libc.so.6", "libz.so.1"])
        self.assertEqual(
            result["symlinks"],
            {
                "/bin/sh": "/usr/bin/dash",
                "/lib64/ld-linux-x86-64.so.2": "/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2",
            },
        )
        self.assertEqual(result["system_paths"], sorted(result["system_paths"]))
        self.assertEqual(result["executable_paths"], sorted(result["executable_paths"]))
        rendered = inventory.render_inventory(result)
        self.assertEqual(json.loads(rendered), result)

    def test_ldconfig_cache_is_read_from_the_requested_root(self) -> None:
        root = self.rootfs()
        command = ["/usr/sbin/ldconfig", "-p", "-r", str(root)]
        completed = mock.Mock(returncode=0, stdout="""\
81 libs found in cache `/etc/ld.so.cache'
\tlibz.so.1 (libc6,x86-64) => /lib/x86_64-linux-gnu/libc.so.6
\tlibc.so.6 (libc6,x86-64) => /lib/x86_64-linux-gnu/libc.so.6
""", stderr="")
        with mock.patch.object(inventory.subprocess, "run", return_value=completed) as run:
            names = inventory.read_ldconfig(root, executable="/usr/sbin/ldconfig")
        run.assert_called_once_with(
            command,
            check=False,
            stdout=inventory.subprocess.PIPE,
            stderr=inventory.subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(names, ("libc.so.6", "libz.so.1"))

    def test_missing_critical_path_fails_closed(self) -> None:
        root = self.rootfs()
        (root / "usr/bin/env").unlink()
        with self.assertRaisesRegex(inventory.InventoryError, "critical launcher path is missing"):
            inventory.build_inventory(root, ldconfig_names=("libc.so.6",))

    def test_non_executable_critical_path_fails_closed(self) -> None:
        root = self.rootfs()
        (root / "usr/bin/dumb-init").chmod(stat.S_IRUSR | stat.S_IWUSR)
        with self.assertRaisesRegex(inventory.InventoryError, "not executable"):
            inventory.build_inventory(root, ldconfig_names=("libc.so.6",))

    def test_stale_soname_target_fails_closed(self) -> None:
        root = self.rootfs()
        with self.assertRaisesRegex(inventory.InventoryError, "missing library"):
            inventory.parse_ldconfig_output(
                "libmissing.so.1 (libc6,x86-64) => /lib/x86_64-linux-gnu/libmissing.so.1\n",
                root=root,
            )

    def test_drift_is_rejected(self) -> None:
        root = self.rootfs()
        expected = inventory.build_inventory(root, ldconfig_names=("libc.so.6",))
        checked_in = dict(expected)
        checked_in["libraries"] = ["libc.so.6", "libdrift.so.1"]
        with mock.patch.object(inventory, "read_ldconfig", return_value=("libc.so.6",)):
            with self.assertRaisesRegex(inventory.InventoryError, "drifted"):
                inventory.validate_inventory_against_root(checked_in, root)

    def test_inventory_shape_rejects_unsorted_or_unknown_fields(self) -> None:
        root = self.rootfs()
        value = inventory.build_inventory(root, ldconfig_names=("libc.so.6",))
        value["libraries"] = ["libz.so.1", "libc.so.6"]
        with self.assertRaisesRegex(inventory.InventoryError, "sorted"):
            inventory.validate_inventory_shape(value)
        invalid = dict(inventory.build_inventory(root, ldconfig_names=("libc.so.6",)))
        invalid["unexpected"] = True
        with self.assertRaises(inventory.InventoryError):
            inventory.validate_inventory_shape(invalid)

    def test_explicit_injected_library_is_recorded_without_a_rootfs_file(self) -> None:
        root = self.rootfs()
        result = inventory.build_inventory(
            root,
            ldconfig_names=("libc.so.6",),
            injected_libraries=("libcuda.so.1",),
        )
        self.assertEqual(result["libraries"], ["libc.so.6", "libcuda.so.1"])

    def test_unsafe_injected_library_is_rejected(self) -> None:
        root = self.rootfs()
        with self.assertRaisesRegex(inventory.InventoryError, "unsafe injected SONAME"):
            inventory.build_inventory(
                root,
                ldconfig_names=("libc.so.6",),
                injected_libraries=("../libcuda.so.1",),
            )


if __name__ == "__main__":
    unittest.main()
