import os
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from adbtool import env_check
from adbtool.adb_client import AdbError, binary_runs
from adbtool.env_check import _safe_extract


def _fake_platform_tools(dest, body=b"#!/bin/sh\nexit 0\n"):
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("platform-tools/adb", body)
        zf.writestr("platform-tools/fastboot", body)


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


class TestBinaryRuns(unittest.TestCase):
    def test_missing_or_broken_binary(self):
        self.assertFalse(binary_runs("/definitely/not/here/adb"))
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "adb")
            with open(p, "wb") as f:
                f.write(b"not a binary")
            os.chmod(p, 0o755)
            self.assertFalse(binary_runs(p))

    def test_working_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "adb")
            with open(p, "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(p, 0o755)
            self.assertTrue(binary_runs(p))


class TestDownloadAdb(unittest.TestCase):
    def test_replaces_unusable_existing_adb(self):
        with tempfile.TemporaryDirectory() as tmp:
            pt = os.path.join(tmp, "platform-tools")
            os.makedirs(pt)
            bad = os.path.join(pt, "adb")
            with open(bad, "wb") as f:
                f.write(b"\xcf\xfa\xed\xfe i386 junk")
            os.chmod(bad, 0o755)

            def fake_download(url, dest, progress=None):
                _fake_platform_tools(dest)

            with (
                mock.patch.object(env_check, "_SDK_DIR", tmp),
                mock.patch.object(env_check, "_URLS", ("https://example.test/pt.zip",)),
                mock.patch.object(env_check, "_download", side_effect=fake_download),
            ):
                got = env_check.download_adb()
            self.assertEqual(got, bad)
            self.assertTrue(binary_runs(bad))

    def test_rejects_downloaded_unusable_adb(self):
        with tempfile.TemporaryDirectory() as tmp:

            def fake_download(url, dest, progress=None):
                _fake_platform_tools(dest, b"broken")

            with (
                mock.patch.object(env_check, "_SDK_DIR", tmp),
                mock.patch.object(env_check, "_URLS", ("https://example.test/a.zip", "https://example.test/b.zip")),
                mock.patch.object(env_check, "_download", side_effect=fake_download),
                self.assertRaises(AdbError),
            ):
                env_check.download_adb()


if __name__ == "__main__":
    unittest.main()
