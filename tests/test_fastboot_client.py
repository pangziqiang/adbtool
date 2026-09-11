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

    def test_looks_custom_kernel_ab_root_init_boot(self):
        # 裴月笙风格：images/Root|NORoot/init_boot.img，内核二选一刷 init_boot_ab
        with tempfile.TemporaryDirectory() as td:
            self._make(td, [], {"Root": "init_boot.img", "NORoot": "init_boot.img"})
            self.assertTrue(self.fb._looks_custom_kernel_ab(td))
        with tempfile.TemporaryDirectory() as td:
            self._make(td, ["init_boot.img"], {"Root": "init_boot.img"})
            self.assertTrue(self.fb._looks_custom_kernel_ab(td))
        with tempfile.TemporaryDirectory() as td:
            self._make(td, ["init_boot.img", "recovery.img"], {"firmware": "vbmeta.img"})
            self.assertFalse(self.fb._looks_custom_kernel_ab(td))

    def test_build_custom_ab_cmds_root_init_boot(self):
        with tempfile.TemporaryDirectory() as td:
            self._make(
                td,
                ["boot.img", "init_boot.img", "recovery.img", "vbmeta.img", "vbmeta_system.img", "super.img.zst"],
                {"Root": "init_boot.img", "NORoot": "init_boot.img"},
            )
            res = self.fb._build_custom_ab_cmds(td, "sheng")
            self.assertEqual(res["source"], "custom_ab")
            parts = [c["args"][1] for c in res["commands"]]
            self.assertIn("boot_ab", parts)
            self.assertIn("init_boot_ab", parts)
            self.assertIn("recovery_ab", parts)
            self.assertIn("super", parts)
            # 子目录内核不自动加入命令，交给 UI 二选一
            self.assertTrue(all("init_boot" != c["args"][1] for c in res["commands"]))
            self.assertEqual([b["part"] for b in res["boot_choices"]], ["init_boot_ab", "init_boot_ab"])
            self.assertEqual([b["label"] for b in res["boot_choices"]], ["Root 内核（KernelSU）", "无 Root 官方内核"])

    def test_parse_package_detects_root_init_boot(self):
        with tempfile.TemporaryDirectory() as pkg:
            os.makedirs(os.path.join(pkg, "bin"), exist_ok=True)
            with open(os.path.join(pkg, "bin", "right_device"), "w") as f:
                f.write("sheng")
            images = os.path.join(pkg, "images")
            os.makedirs(images, exist_ok=True)
            self._make(
                images,
                ["boot.img", "init_boot.img", "super.img.zst"],
                {"Root": "init_boot.img", "NORoot": "init_boot.img"},
            )
            res = self.fb.parse_package(pkg)
            self.assertEqual(res["source"], "custom_ab")
            self.assertEqual(res["right_device"], "sheng")
            parts = [c["args"][1] for c in res["commands"]]
            self.assertIn("boot_ab", parts)
            self.assertIn("init_boot_ab", parts)
            self.assertEqual(len(res["boot_choices"]), 2)

    def test_traverse_arbitrary_kernel_dir_names(self):
        # 目录名不固定：中文 / 自定义命名也要能按关键字归类
        with tempfile.TemporaryDirectory() as td:
            self._make(
                td,
                ["init_boot.img", "super.img.zst"],
                {"有Root内核": "init_boot.img", "官方原厂": "init_boot.img"},
            )
            res = self.fb._build_kernel_choices(td)
            self.assertEqual(
                [(b["label"], b["part"], b["dir"]) for b in res],
                [
                    ("Root 内核（KernelSU）", "init_boot_ab", "有Root内核"),
                    ("无 Root 官方内核", "init_boot_ab", "官方原厂"),
                ],
            )

    def test_traverse_nested_kernel_dir(self):
        # 内核放在多层子目录里也要能遍历到
        with tempfile.TemporaryDirectory() as td:
            self._make(
                td,
                ["boot.img", "super.img.zst"],
                {"inner/Rootkernel": "boot.img", "inner/kernel": "boot.img"},
            )
            res = self.fb._build_kernel_choices(td)
            self.assertEqual([b["part"] for b in res], ["boot_ab", "boot_ab"])
            self.assertEqual(
                [(b["label"], b["dir"]) for b in res],
                [
                    ("Root 内核（KernelSU）", "inner/Rootkernel"),
                    ("无 Root 官方内核", "inner/kernel"),
                ],
            )

    def test_traverse_unknown_dir_lists_as_choice(self):
        # 关键字认不出时，按目录名列出来交给用户判断
        with tempfile.TemporaryDirectory() as td:
            self._make(td, ["init_boot.img"], {"aaa": "init_boot.img", "bbb": "init_boot.img"})
            res = self.fb._build_kernel_choices(td)
            self.assertEqual(len(res), 2)
            self.assertTrue(all(b["label"].startswith("内核：") for b in res))
