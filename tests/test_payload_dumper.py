import os
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from adbtool.payload_dumper import (
    PayloadError,
    download,
    extract_partitions,
    extract_payload_from_zip,
    is_payload,
)


class TestPayloadDumper(unittest.TestCase):
    def test_is_payload_true(self):
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(b"CrAU" + b"\x00" * 24)
            path = f.name
        try:
            self.assertTrue(is_payload(path))
        finally:
            os.remove(path)

    def test_is_payload_false(self):
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(b"PK\x03\x04" + b"\x00" * 16)
            path = f.name
        try:
            self.assertFalse(is_payload(path))
        finally:
            os.remove(path)

    def test_is_payload_missing(self):
        self.assertFalse(is_payload("/nonexistent/payload.bin"))

    def test_extract_payload_from_zip(self):
        with tempfile.TemporaryDirectory() as td:
            zpath = os.path.join(td, "rom.zip")
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("payload.bin", b"CrAU-data")
            out = os.path.join(td, "out")
            target = extract_payload_from_zip(zpath, out)
            self.assertTrue(os.path.isfile(target))
            with open(target, "rb") as f:
                self.assertEqual(f.read(), b"CrAU-data")

    def test_extract_payload_from_zip_no_payload(self):
        with tempfile.TemporaryDirectory() as td:
            zpath = os.path.join(td, "rom.zip")
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("README.txt", b"x")
            with self.assertRaises(PayloadError):
                extract_payload_from_zip(zpath, os.path.join(td, "out"))

    def test_extract_partitions_command(self):
        called = {}

        class FakePopen:
            def __init__(self, args, **kw):
                called["args"] = args
                called["kw"] = kw
                self.stdout = iter(())
                self.returncode = 0

            def wait(self, timeout=None):
                return 0

        with (
            mock.patch("adbtool.payload_dumper.ensure_payload_dumper", return_value="/x/payload-dumper-go"),
            mock.patch("adbtool.payload_dumper.subprocess.Popen", FakePopen),
            tempfile.TemporaryDirectory() as td,
        ):
            extract_partitions("/x/payload.bin", td, ["boot", "init_boot"])
        self.assertEqual(
            called["args"],
            ["/x/payload-dumper-go", "-partitions", "boot,init_boot", "-o", td, "/x/payload.bin"],
        )

    def test_download_progress(self):
        class FakeResp:
            def __init__(self):
                self._data = b"1234567890"
                self._i = 0
                self.headers = {"Content-Length": "10"}

            def read(self, _n):
                if self._i >= len(self._data):
                    return b""
                chunk = self._data[self._i : self._i + _n]
                self._i += len(chunk)
                return chunk

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        seen = []
        with (
            mock.patch("adbtool.payload_dumper.urllib.request.urlopen", return_value=FakeResp()),
            tempfile.TemporaryDirectory() as td,
        ):
            dest = download("https://x/rom.zip", os.path.join(td, "rom.zip"), progress=lambda d, t: seen.append((d, t)))
            with open(dest, "rb") as f:
                self.assertEqual(f.read(), b"1234567890")
        self.assertEqual(seen[-1], (10, 10))


if __name__ == "__main__":
    unittest.main()
