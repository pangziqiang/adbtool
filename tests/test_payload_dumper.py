import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from adbtool.payload_dumper import is_payload


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


if __name__ == "__main__":
    unittest.main()
