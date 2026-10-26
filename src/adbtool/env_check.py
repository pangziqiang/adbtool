from __future__ import annotations

import os
import shutil
import subprocess
import time
import urllib.request
import zipfile

from .adb_client import AdbClient, AdbError, binary_runs

_OFFICIAL_URL = "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip"
_MIRROR_URL = "https://mirrors.cloud.tencent.com/AndroidSDK/platform-tools-latest-darwin.zip"
_URLS = (_OFFICIAL_URL, _MIRROR_URL)
_SDK_DIR = os.path.expanduser("~/Library/Android/sdk")
_DOWNLOAD_TIMEOUT = 150  # 单个下载源的总时长上限（秒）


def ensure_adb(progress=None) -> str:
    try:
        return AdbClient().adb_path
    except AdbError:
        return download_adb(progress)


def download_adb(progress=None) -> str:
    os.makedirs(_SDK_DIR, exist_ok=True)
    adb = os.path.join(_SDK_DIR, "platform-tools", "adb")
    # 必须真实可执行：旧的 i386 版本在 64 位 macOS 上无法运行
    if binary_runs(adb):
        return adb
    zip_path = os.path.join(_SDK_DIR, "platform-tools.zip")
    errors: list[str] = []
    for url in _URLS:
        host = url.split("/")[2]
        try:
            if progress:
                progress(f"正在下载 platform-tools（{host}）...")
            _download(url, zip_path, progress=progress)
            _safe_extract(zip_path, _SDK_DIR)
            for tool in ("adb", "fastboot"):
                p = os.path.join(_SDK_DIR, "platform-tools", tool)
                if os.path.isfile(p):
                    os.chmod(p, 0o755)
            try:
                subprocess.run(
                    ["xattr", "-d", "com.apple.quarantine", adb],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            except OSError:
                pass
            if not binary_runs(adb):
                raise RuntimeError("下载的 adb 无法在本机运行（可能是架构不匹配）")
            try:
                os.remove(zip_path)
            except OSError:
                pass
            return adb
        except Exception as e:
            errors.append(f"{host}: {e}")
            if progress:
                progress(f"下载失败（{host}）：{e}")
            try:
                os.remove(zip_path)
            except OSError:
                pass
    raise AdbError("自动下载 adb 失败：" + ("；".join(errors) or "未知错误") + "。请手动安装 platform-tools")


def _download(url: str, dest: str, progress=None) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "adbtool/0.2"})
    deadline = time.monotonic() + _DOWNLOAD_TIMEOUT
    downloaded = 0
    reported = 0
    with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError(f"下载超时（{_DOWNLOAD_TIMEOUT} 秒）")
            chunk = resp.read(65536)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            if progress and downloaded - reported >= 2 * 1024 * 1024:
                reported = downloaded
                if total:
                    progress(f"已下载 {downloaded / 1048576:.1f}/{total / 1048576:.1f} MB")
                else:
                    progress(f"已下载 {downloaded / 1048576:.1f} MB")
    if downloaded == 0:
        raise RuntimeError("下载内容为空")


def _safe_extract(zip_path: str, dest: str) -> None:
    """安全解压：限制条目数与总大小，并阻止 zip-slip 路径穿越。"""
    base = os.path.abspath(dest)
    os.makedirs(base, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        if len(infos) > 5000:
            raise RuntimeError("zip 条目过多，已中止解压")
        if sum(m.file_size for m in infos) > 2 * 1024**3:
            raise RuntimeError("zip 解压总大小超过 2GB，已中止解压")
        for m in infos:
            if m.is_dir():
                continue
            target = os.path.abspath(os.path.join(base, m.filename))
            if not target.startswith(base + os.sep):
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(m) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
