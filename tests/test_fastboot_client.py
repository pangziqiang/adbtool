import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from adbtool.fastboot_client import FastbootClient, FastbootError


class TestFastbootClient(unittest.TestCase):
    def setUp(self):
        self.fb = FastbootClient(fastboot_path="/bin/echo")

    def _make_images(self, td, names):
        for n in names:
            with open(os.path.join(td, n), "wb") as f:
                f.write(b"x")

    def test_build_images_cmds_aonly(self):
        with tempfile.TemporaryDirectory() as td:
            self._make_images(td, ["boot.img", "recovery.img", "super.img", "vbmeta.img"])
            res = self.fb._build_images_cmds(td, "")
            self.assertFalse(res["ab"])
            parts = [c["args"][1] for c in res["commands"]]
            self.assertIn("boot", parts)
            self.assertIn("recovery", parts)
            self.assertIn("super", parts)
            self.assertIn("vbmeta", parts)
            # super 单独附加，每个 flash 命令分区名唯一
            self.assertEqual(len(parts), len(set(parts)))

    def test_build_images_cmds_ab(self):
        with tempfile.TemporaryDirectory() as td:
            self._make_images(td, ["boot_ab.img", "super.img"])
            res = self.fb._build_images_cmds(td, "")
            self.assertTrue(res["ab"])
            parts = [c["args"][1] for c in res["commands"]]
            self.assertIn("boot_a", parts)
            self.assertIn("boot_b", parts)
            # A/B 双槽末尾应有 set_active a
            self.assertTrue(any(c["args"][0] == "--set-active" for c in res["commands"]))

    def test_parse_zip_blocks_path_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            zip_path = os.path.join(td, "pkg.zip")
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("../evil.txt", "HACK")
                zf.writestr("images/boot.img", "boot-image")
            res = self.fb.parse_zip(zip_path)
            out = os.path.join(td, ".pkg_extracted")
            # 路径穿越被过滤，evil 不应写到 zip 目录外
            self.assertFalse(os.path.exists(os.path.join(td, "evil.txt")))
            # 正常镜像被解出并被识别
            self.assertTrue(os.path.isfile(os.path.join(out, "images", "boot.img")))
            parts = [c["args"][1] for c in res["commands"]]
            self.assertIn("boot", parts)

    def test_parse_zip_entry_limit(self):
        with tempfile.TemporaryDirectory() as td:
            zip_path = os.path.join(td, "many.zip")
            with zipfile.ZipFile(zip_path, "w") as zf:
                for i in range(6000):
                    zf.writestr(f"f{i}.txt", "x")
            with self.assertRaises(FastbootError):
                self.fb.parse_zip(zip_path)


if __name__ == "__main__":
    unittest.main()


class TestCustomAbPackage(unittest.TestCase):
    def setUp(self):
        self.fb = FastbootClient(fastboot_path="/bin/echo")

    def _make(self, td, files, subdirs=None):
        for n in files:
            p = os.path.join(td, n)
            os.makedirs(os.path.dirname(p), exist_ok=True) if os.path.dirname(n) else None
            with open(p, "wb") as f:
                f.write(b"x")
        for sub, boot in (subdirs or {}).items():
            d = os.path.join(td, sub)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, boot), "wb") as f:
                f.write(b"x")

    def test_looks_custom_kernel_ab(self):
        with tempfile.TemporaryDirectory() as td:
            self._make(td, ["boot.img"], {"Rootkernel": "boot.img"})
            self.assertTrue(self.fb._looks_custom_kernel_ab(td))
        with tempfile.TemporaryDirectory() as td:
            self._make(td, ["boot.img", "kernel/boot.img"])
            self.assertTrue(self.fb._looks_custom_kernel_ab(td))
        with tempfile.TemporaryDirectory() as td:
            self._make(td, ["boot.img", "recovery.img"])
            self.assertFalse(self.fb._looks_custom_kernel_ab(td))

    def test_build_custom_ab_cmds(self):
        with tempfile.TemporaryDirectory() as td:
            self._make(
                td,
                ["init_boot.img", "recovery.img", "vbmeta.img", "vbmeta_system.img", "super.img.zst"],
                {"Rootkernel": "boot.img", "kernel": "boot.img"},
            )
            res = self.fb._build_custom_ab_cmds(td, "sheng")
            self.assertEqual(res["source"], "custom_ab")
            self.assertTrue(res["ab"])
            self.assertEqual(res["right_device"], "sheng")
            parts = [c["args"][1] for c in res["commands"]]
            # 顶层镜像刷到 _ab 槽；super 单分区
            self.assertIn("init_boot_ab", parts)
            self.assertIn("recovery_ab", parts)
            self.assertIn("vbmeta_ab", parts)
            self.assertIn("vbmeta_system_ab", parts)
            self.assertIn("super", parts)
            self.assertNotIn("init_boot", parts)  # 不应出现无槽分区名
            # boot 由内核子目录提供，不自动加入命令
            self.assertTrue(all("boot" != c["args"][1] for c in res["commands"]))
            self.assertEqual(len(res["boot_choices"]), 2)
            labels = {b["label"] for b in res["boot_choices"]}
            self.assertIn("无 Root 官方内核", labels)

    def test_parse_package_detects_custom_ab(self):
        with tempfile.TemporaryDirectory() as pkg:
            os.makedirs(os.path.join(pkg, "bin"), exist_ok=True)
            with open(os.path.join(pkg, "bin", "right_device"), "w") as f:
                f.write("sheng")
            images = os.path.join(pkg, "images")
            os.makedirs(images, exist_ok=True)
            self._make(
                images,
                ["init_boot.img", "recovery.img", "super.img.zst"],
                {"Rootkernel": "boot.img"},
            )
            res = self.fb.parse_package(pkg)
            self.assertEqual(res["source"], "custom_ab")
            parts = [c["args"][1] for c in res["commands"]]
            self.assertIn("init_boot_ab", parts)
            self.assertEqual(len(res["boot_choices"]), 1)
