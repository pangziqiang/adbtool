import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from adbtool.adb_client import AdbClient, AdbError

FAKE_LS = """total 100
drwxr-xr-x 1 root root 4096 2026-08-09 19:35 .hidden
drwxr-xr-x 2 u0_a1 media_rw 3452 2026-08-01 12:00 DCIM
-rw-rw---- 1 u0_a1 media_rw 10240 2026-08-17 04:37 photo.jpg
lrwxrwxrwx 1 root root 21 2009-01-01 08:00 link -> /storage/self/primary
"""


class TestAdbClient(unittest.TestCase):
    def setUp(self):
        self.adb = AdbClient()
        self.adb.device = "emulator-5554"

    def _fake_run(self, output):
        proc = subprocess.CompletedProcess([], 0, stdout=output)
        with patch.object(self.adb, "_run", return_value=output):
            pass
        return patch.object(self.adb, "_run", return_value=output)

    def test_list_dir_parses_toybox(self):
        with self._fake_run(FAKE_LS):
            entries = self.adb.list_dir("/sdcard")
        by_name = {e.name: e for e in entries}
        self.assertEqual(len(entries), 4)
        self.assertTrue(by_name["DCIM"].is_dir)
        self.assertFalse(by_name["photo.jpg"].is_dir)
        self.assertEqual(by_name["photo.jpg"].size, 10240)
        self.assertEqual(by_name["photo.jpg"].mtime, "2026-08-17 04:37")
        self.assertEqual(by_name["link"].path, "/sdcard/link")
        self.assertIn(".hidden", by_name, "dot entries preserved")

    def test_list_dir_sorts_dirs_first(self):
        with self._fake_run(FAKE_LS):
            entries = self.adb.list_dir("/sdcard")
        self.assertTrue(entries[0].is_dir)

    def test_list_devices_parses(self):
        out = "List of devices attached\n192.168.1.14:5555  device product:houji model:23127PN0CC device:houji\n"
        with self._fake_run(out):
            devices = self.adb.list_devices()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].serial, "192.168.1.14:5555")
        self.assertEqual(devices[0].model, "23127PN0CC")

    def test_error_raises(self):
        proc = subprocess.CompletedProcess([], 1, stdout="", stderr="not found")
        with patch.object(self.adb, "_run", side_effect=AdbError("not found")):
            with self.assertRaises(AdbError):
                self.adb.list_dir("/nope")


if __name__ == "__main__":
    unittest.main()