from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import tarfile
import urllib.request
from typing import Callable

_REPO = "ssut/payload-dumper-go"
_VERSION = "2.0.2"
_SDK_DIR = os.path.expanduser("~/Library/Android/sdk")
_TOOL_NAME = "payload-dumper-go"
_SHA256 = {
    "amd64": "bed075dd489d8b102c9d6acd77e7a3015972073f85faedc906218e1873070bd4",
    "arm64": "05d908e0dc083f668a8b35dd511d096dc14b219842a64c92b94f7a38383fedfe",
}


class PayloadError(Exception):
    pass


def _arch() -> str:
    machine = platform.machine().lower()
    return "arm64" if machine in ("arm64", "aarch64") else "amd64"


def _release_url() -> str:
    arch = _arch()
    return (
        f"https://github.com/{_REPO}/releases/download/{_VERSION}/"
        f"payload-dumper-go_{_VERSION}_darwin_{arch}.tar.gz"
    )


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _installed_path() -> str | None:
    for p in (
        shutil.which(_TOOL_NAME),
        "/usr/local/bin/payload-dumper-go",
        "/opt/homebrew/bin/payload-dumper-go",
        os.path.join(_SDK_DIR, _TOOL_NAME),
    ):
        if p and os.path.isfile(p):
            return p
    return None


def _download(url: str, dest: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "adbtool/0.1"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            f.write(chunk)


def _remove_quarantine(path: str) -> None:
    try:
        subprocess.run(
            ["xattr", "-d", "com.apple.quarantine", path],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except OSError:
        pass


def ensure_payload_dumper(progress: Callable[[str], None] | None = None) -> str:
    p = _installed_path()
    if p:
        return p
    os.makedirs(_SDK_DIR, exist_ok=True)
    exe = os.path.join(_SDK_DIR, _TOOL_NAME)
    url = _release_url()
    if progress:
        progress(f"正在下载 payload-dumper-go（v{_VERSION}）...")
    tgz = os.path.join(_SDK_DIR, "payload-dumper-go.tar.gz")
    try:
        _download(url, tgz)
        expected = _SHA256.get(_arch())
        if expected and _sha256_file(tgz) != expected:
            raise PayloadError("下载校验失败：SHA-256 与官方发布不一致")
        with tarfile.open(tgz, "r:gz") as tf:
            for m in tf.getmembers():
                if m.isfile() and m.name.endswith(_TOOL_NAME):
                    with tf.extractfile(m) as src, open(exe, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    break
            else:
                raise PayloadError("下载的压缩包中没有 payload-dumper-go")
        os.remove(tgz)
        os.chmod(exe, 0o755)
        _remove_quarantine(exe)
        return exe
    except Exception as e:
        if os.path.exists(tgz):
            try:
                os.remove(tgz)
            except OSError:
                pass
        raise PayloadError(f"自动下载 payload-dumper-go 失败: {e}") from None


def is_payload(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"CrAU"
    except OSError:
        return False


def list_partitions(payload_path: str) -> list[str]:
    tool = ensure_payload_dumper()
    proc = subprocess.run(
        [tool, "-list", payload_path],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise PayloadError(proc.stderr.strip() or "列出 payload 分区失败")
    parts: list[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line and not line.startswith("["):
            parts.append(line)
    return parts


def extract(
    payload_path: str,
    out_dir: str,
    line_cb: Callable[[str], None] | None = None,
) -> None:
    tool = ensure_payload_dumper()
    os.makedirs(out_dir, exist_ok=True)
    proc = subprocess.Popen(
        [tool, "-o", out_dir, payload_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        if line_cb:
            line_cb(line.rstrip())
    proc.wait(timeout=3600)
    if proc.returncode != 0:
        raise PayloadError("payload 解包失败")
