import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from adbtool.env_check import _safe_extract


class TestSafeExtract(unittest.TestCase):
    def test_extract_normal_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            zpath = os.path.join(tmp, "a.zip")
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("platform-tools/adb", b"#!/bin/sh\n")
                zf.writestr("platform-tools/fastboot", b"#!/bin/sh\n")
            _safe_extract(zpath, tmp)
            self.assertTrue(os.path.isfile(os.path.join(tmp, "platform-tools", "adb")))
            self.assertTrue(os.path.isfile(os.path.join(tmp, "platform-tools", "fastboot")))

    def test_blocks_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            zpath = os.path.join(tmp, "evil.zip")
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("../../../etc/pwned", b"evil")
            _safe_extract(zpath, tmp)
            # 不应解出到 dest 之外
            self.assertFalse(os.path.exists(os.path.join(tmp, "..", "..", "..", "etc", "pwned")))
            # dest 内也不应创建越界条目
            for root, _dirs, files in os.walk(tmp):
                self.assertFalse(any(f == "pwned" for f in files))

    def test_entry_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            zpath = os.path.join(tmp, "many.zip")
            with zipfile.ZipFile(zpath, "w") as zf:
                for i in range(5001):
                    zf.writestr(f"f{i}", b"x")
            with self.assertRaises(RuntimeError):
                _safe_extract(zpath, tmp)


if __name__ == "__main__":
    unittest.main()
