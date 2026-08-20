from __future__ import annotations

import os
import subprocess
import urllib.request
import zipfile

from .adb_client import AdbClient, AdbError

_OFFICIAL_URL = (
    "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip"
)
_FALLBACK_URL = (
    "https://mirrors.huaweicloud.com/android/repository/"
    "platform-tools_r23.0.1-macosx.zip"
)
_SDK_DIR = os.path.expanduser("~/Library/Android/sdk")


def ensure_adb(progress=None) -> str:
    try:
        return AdbClient().adb_path
    except AdbError:
        return download_adb(progress)


def download_adb(progress=None) -> str:
    os.makedirs(_SDK_DIR, exist_ok=True)
    adb = os.path.join(_SDK_DIR, "platform-tools", "adb")
    if os.path.isfile(adb):
        return adb
    zip_path = os.path.join(_SDK_DIR, "platform-tools.zip")
    for url in (_OFFICIAL_URL, _FALLBACK_URL):
        try:
            if progress:
                progress(f"正在下载 adb（{url.split('/')[2]}）...")
            _download(url, zip_path)
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(_SDK_DIR)
            os.remove(zip_path)
            for tool in ("adb", "fastboot"):
                p = os.path.join(_SDK_DIR, "platform-tools", tool)
                if os.path.isfile(p):
                    os.chmod(p, 0o755)
            try:
                subprocess.run(
                    ["xattr", "-d", "com.apple.quarantine", adb],
                    capture_output=True,
                    timeout=10,
                )
            except OSError:
                pass
            return adb
        except Exception as e:
            if progress:
                progress(f"下载失败（{url.split('/')[2]}）：{e}")
    raise AdbError("自动下载 adb 失败，请手动安装 platform-tools")


def _download(url: str, dest: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "adbpush/0.1"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            f.write(chunk)