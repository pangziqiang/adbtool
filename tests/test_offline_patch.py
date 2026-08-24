import os
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from adbtool import offline_patch
from adbtool.offline_patch import (
    PatchError,
    build_gki,
    extract_magisk,
    is_boot_image,
    is_magisk_apk,
    patch_magisk,
    patch_skroot,
)


def _make_boot(path):
    with open(path, "wb") as f:
        f.write(b"ANDROID!" + b"\x00" * 24)


def _make_magisk_apk(path, abi="arm64-v8a"):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("assets/boot_patch.sh", "#!/system/bin/sh\necho patch\n")
        zf.writestr("assets/util_functions.sh", "echo util\n")
        zf.writestr(f"lib/{abi}/libmagiskboot.so", b"ELF-magiskboot")
        zf.writestr(f"lib/{abi}/libmagiskinit.so", b"ELF-init")
        zf.writestr(f"lib/{abi}/libmagisk32.so", b"ELF-32")
        zf.writestr(f"lib/{abi}/libmagisk64.so", b"ELF-64")
        zf.writestr(f"lib/{abi}/libmagiskpolicy.so", b"ELF-policy")
        zf.writestr(f"lib/{abi}/libbusybox.so", b"ELF-busybox")


class TestOfflinePatch(unittest.TestCase):
    def test_is_boot_image(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "boot.img")
            _make_boot(p)
            self.assertTrue(is_boot_image(p))
        self.assertFalse(is_boot_image("/nonexistent/boot.img"))

    def test_is_magisk_apk(self):
        with tempfile.TemporaryDirectory() as td:
            apk = os.path.join(td, "magisk.apk")
            _make_magisk_apk(apk)
            self.assertTrue(is_magisk_apk(apk))
            self.assertFalse(is_magisk_apk(__file__))

    def test_extract_magisk_renames(self):
        with tempfile.TemporaryDirectory() as td:
            apk = os.path.join(td, "magisk.apk")
            _make_magisk_apk(apk)
            out = os.path.join(td, "bin")
            extract_magisk(apk, out)
            self.assertTrue(os.path.isfile(os.path.join(out, "boot_patch.sh")))
            self.assertTrue(os.path.isfile(os.path.join(out, "util_functions.sh")))
            self.assertTrue(os.path.isfile(os.path.join(out, "magiskboot")))
            self.assertTrue(os.path.isfile(os.path.join(out, "busybox")))
            with open(os.path.join(out, "magiskboot"), "rb") as f:
                self.assertEqual(f.read(), b"ELF-magiskboot")

    def test_extract_magisk_bad_abi(self):
        with tempfile.TemporaryDirectory() as td, self.assertRaises(PatchError):
            extract_magisk("x.apk", os.path.join(td, "o"), abi="bad")

    def test_extract_magisk_not_apk(self):
        with tempfile.TemporaryDirectory() as td, self.assertRaises(PatchError):
            extract_magisk(__file__, os.path.join(td, "o"))

    def test_patch_magisk_bad_boot(self):
        with tempfile.TemporaryDirectory() as td:
            apk = os.path.join(td, "m.apk")
            _make_magisk_apk(apk)
            bad = os.path.join(td, "bad.img")
            with open(bad, "wb") as f:
                f.write(b"not a boot image")
            with self.assertRaises(PatchError):
                patch_magisk(bad, apk, os.path.join(td, "out"))

    def test_patch_magisk_runs_script(self):
        with tempfile.TemporaryDirectory() as td:
            apk = os.path.join(td, "m.apk")
            _make_magisk_apk(apk)
            boot = os.path.join(td, "boot.img")
            _make_boot(boot)
            out = os.path.join(td, "out")
            real_mkdtemp = offline_patch.tempfile.mkdtemp
            created = {}

            def fake_mkdtemp(prefix=""):
                work = real_mkdtemp(prefix=prefix)
                created["work"] = work
                return work

            with (
                mock.patch.object(offline_patch.tempfile, "mkdtemp", side_effect=fake_mkdtemp),
                mock.patch.object(offline_patch.shutil, "which", return_value="/usr/local/bin/magiskboot"),
            ):
                real_copy = offline_patch.shutil.copy

                def fake_copy(src, dst):
                    if src == "/usr/local/bin/magiskboot":
                        with open(dst, "wb") as f:
                            f.write(b"native")
                        return dst
                    return real_copy(src, dst)

                with mock.patch.object(offline_patch.shutil, "copy", side_effect=fake_copy):

                    def fake_run(cmd, **kw):
                        bin_dir = os.path.join(created["work"], "bin")
                        os.makedirs(bin_dir, exist_ok=True)
                        with open(os.path.join(bin_dir, "new-boot.img"), "wb") as f:
                            f.write(b"patched")
                        return mock.Mock(returncode=0, stdout="All done!\n", stderr="")

                    with mock.patch.object(offline_patch.subprocess, "run", side_effect=fake_run) as run:
                        dst = patch_magisk(boot, apk, out)
            work = created["work"]
            args = run.call_args[0][0]
            self.assertEqual(args, ["sh", "boot_patch.sh", os.path.join(work, "stock_boot.img")])
            self.assertEqual(run.call_args[1]["cwd"], os.path.join(work, "bin"))
            self.assertEqual(run.call_args[1]["env"]["KEEPVERITY"], "true")
            self.assertTrue(os.path.isfile(dst))
            with open(dst, "rb") as f:
                self.assertEqual(f.read(), b"patched")

    def test_patch_skroot_unsupported(self):
        with tempfile.TemporaryDirectory() as td, self.assertRaises(PatchError):
            patch_skroot(os.path.join(td, "boot.img"), os.path.join(td, "out"))

    def test_build_gki(self):
        with tempfile.TemporaryDirectory() as td:
            boot = os.path.join(td, "boot.img")
            _make_boot(boot)
            ak3 = os.path.join(td, "ak3.zip")
            with zipfile.ZipFile(ak3, "w") as zf:
                zf.writestr("anykernel.sh", "# ak3")
                zf.writestr("Image", b"kernel-bytes")
            out = os.path.join(td, "out")
            real_mkdtemp = offline_patch.tempfile.mkdtemp
            created = {}

            def fake_mkdtemp(prefix=""):
                work = real_mkdtemp(prefix=prefix)
                created["work"] = work
                return work

            def fake_run(cmd, **kw):
                if "repack" in cmd:
                    bdir = os.path.join(created["work"], "boot")
                    os.makedirs(bdir, exist_ok=True)
                    with open(os.path.join(bdir, "new-boot.img"), "wb") as f:
                        f.write(b"newboot")
                return mock.Mock(returncode=0, stdout="", stderr="")

            with (
                mock.patch.object(offline_patch.tempfile, "mkdtemp", side_effect=fake_mkdtemp),
                mock.patch.object(offline_patch, "_find_magiskboot", return_value="/x/magiskboot"),
                mock.patch.object(offline_patch.subprocess, "run", side_effect=fake_run),
            ):
                dst = build_gki(boot, ak3, out)
            self.assertTrue(os.path.isfile(dst))

    def test_build_gki_missing_magiskboot(self):
        with tempfile.TemporaryDirectory() as td:
            boot = os.path.join(td, "boot.img")
            _make_boot(boot)
            ak3 = os.path.join(td, "ak3.zip")
            with zipfile.ZipFile(ak3, "w") as zf:
                zf.writestr("Image", b"k")
            with mock.patch.object(offline_patch.shutil, "which", return_value=None), self.assertRaises(PatchError):
                build_gki(boot, ak3, os.path.join(td, "out"))


if __name__ == "__main__":
    unittest.main()
