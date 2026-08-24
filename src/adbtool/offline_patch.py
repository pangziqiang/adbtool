"""脱机修补：Magisk 修补 / 制作 GKI 镜像 / SK-ROOT。

Magisk 修补流程（参照官方 boot_patch.sh）：
1. 解包 Magisk APK 的 assets 脚本 + lib/<abi> 下的 so 二进制；
2. 将 so 重命名为 magiskboot/magiskinit/magisk32/magisk64/magiskpolicy/busybox；
3. 优先用宿主系统里可运行的 magiskboot 覆盖（macOS 上 APK 内的 arm 二进制无法直接运行）；
4. 以脚本目录为工作目录运行 `sh boot_patch.sh <绝对路径 boot.img>`，产物为 new-boot.img。

制作 GKI 镜像：用 AnyKernel3（AK3）内的内核替换 boot.img 的内核后重新打包。
SK-ROOT 为厂商（三星 KNOX）私有方案，通用实现尚不支持。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import zipfile
from typing import Callable

_BOOT_MAGIC = b"ANDROID!"

# Magisk APK 内需要解出的资产：目标文件名 -> APK 内路径（{abi} 为 ABI 占位）
_MAGISK_ASSETS = {
    "boot_patch.sh": "assets/boot_patch.sh",
    "util_functions.sh": "assets/util_functions.sh",
    "magiskboot": "lib/{abi}/libmagiskboot.so",
    "magiskinit": "lib/{abi}/libmagiskinit.so",
    "magisk32": "lib/{abi}/libmagisk32.so",
    "magisk64": "lib/{abi}/libmagisk64.so",
    "magiskpolicy": "lib/{abi}/libmagiskpolicy.so",
    "busybox": "lib/{abi}/libbusybox.so",
}

_SUPPORTED_ABIS = ("arm64-v8a", "armeabi-v7a", "x86_64", "x86")


class PatchError(Exception):
    pass


def is_boot_image(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(8) == _BOOT_MAGIC
    except OSError:
        return False


def is_magisk_apk(path: str) -> bool:
    try:
        with zipfile.ZipFile(path) as zf:
            return "assets/boot_patch.sh" in zf.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def _resolve_magisk_assets(abi: str) -> dict[str, str]:
    return {target: member.format(abi=abi) for target, member in _MAGISK_ASSETS.items()}


def extract_magisk(
    apk: str, out_dir: str, abi: str = "arm64-v8a", line_cb: Callable[[str], None] | None = None
) -> None:
    """把 Magisk APK 的修补脚本和二进制解到 out_dir，二进制按 boot_patch.sh 需要的名字重命名。"""
    if abi not in _SUPPORTED_ABIS:
        raise PatchError(f"不支持的 ABI: {abi}")
    if not is_magisk_apk(apk):
        raise PatchError("不是有效的 Magisk APK（缺少 assets/boot_patch.sh）")
    with zipfile.ZipFile(apk) as zf:
        names = set(zf.namelist())
        mapping = _resolve_magisk_assets(abi)
        for member in mapping.values():
            if member not in names:
                raise PatchError(f"APK 中缺少 {member}，无法离线修补")
        os.makedirs(out_dir, exist_ok=True)
        for target, member in mapping.items():
            if line_cb:
                line_cb(f"解出 {member} → {target}")
            with zf.open(member) as src, open(os.path.join(out_dir, target), "wb") as dst:
                shutil.copyfileobj(src, dst)
    for name in os.listdir(out_dir):
        try:
            os.chmod(os.path.join(out_dir, name), 0o755)
        except OSError:
            pass


def patch_magisk(
    boot_img: str,
    apk: str,
    out_dir: str,
    options: dict | None = None,
    line_cb: Callable[[str], None] | None = None,
) -> str:
    """离线 Magisk 修补 boot/init_boot，返回修补后镜像路径。"""
    if not is_boot_image(boot_img):
        raise PatchError("不是有效的 boot/init_boot 镜像（缺少 ANDROID! 魔数）")
    options = options or {}
    abi = options.get("abi", "arm64-v8a")
    keep_verity = options.get("keep_verity", True)
    keep_force_encrypt = options.get("keep_force_encrypt", True)
    recovery = options.get("recovery", False)
    patch_vbmeta = options.get("patch_vbmeta", False)
    force_rootfs = options.get("force_rootfs", False)
    legacy_sar = options.get("legacy_sar", False)

    work = tempfile.mkdtemp(prefix="adbtool_magisk_")
    try:
        bin_dir = os.path.join(work, "bin")
        extract_magisk(apk, bin_dir, abi=abi, line_cb=line_cb)
        # 优先用宿主系统里可运行的 magiskboot（macOS 上 APK 内的是 Android 二进制）
        native = shutil.which("magiskboot")
        if native:
            shutil.copy(native, os.path.join(bin_dir, "magiskboot"))
        stock = os.path.join(work, "stock_boot.img")
        shutil.copy(boot_img, stock)
        env = dict(os.environ)
        env.update(
            {
                "KEEPVERITY": "true" if keep_verity else "false",
                "KEEPFORCEENCRYPT": "true" if keep_force_encrypt else "false",
                "RECOVERYMODE": "true" if recovery else "false",
                "PATCHVBMETAFLAG": "true" if patch_vbmeta else "false",
                "LEGACYSAR": "true" if legacy_sar else "false",
            }
        )
        if force_rootfs:
            env["FORCEROOTFS"] = "true"
        if line_cb:
            line_cb("正在运行 boot_patch.sh（可能需数分钟）...")
        proc = subprocess.run(
            ["sh", "boot_patch.sh", stock],
            cwd=bin_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=3600,
            check=False,
        )
        if line_cb:
            for l in (proc.stdout or "").splitlines():
                line_cb(l)
            for l in (proc.stderr or "").splitlines():
                line_cb(l)
        if proc.returncode != 0:
            tail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-5:]
            raise PatchError("boot_patch.sh 执行失败：" + " | ".join(tail))
        patched = os.path.join(bin_dir, "new-boot.img")
        if not os.path.isfile(patched):
            raise PatchError("修补后未找到 new-boot.img")
        os.makedirs(out_dir, exist_ok=True)
        base = os.path.splitext(os.path.basename(boot_img))[0]
        dst = os.path.join(out_dir, f"{base}-magisk-patched.img")
        shutil.copy(patched, dst)
        return dst
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _find_magiskboot() -> str:
    p = shutil.which("magiskboot")
    if not p:
        raise PatchError("未找到 magiskboot，请先用 brew install magiskboot 或放入 PATH")
    return p


def _unpack_ak3(ak3_zip: str, out_dir: str, line_cb: Callable[[str], None] | None = None) -> None:
    with zipfile.ZipFile(ak3_zip) as zf:
        infos = zf.infolist()
        if len(infos) > 2000:
            raise PatchError("AK3 压缩包条目过多，已中止")
        os.makedirs(out_dir, exist_ok=True)
        base = os.path.abspath(out_dir)
        for m in infos:
            if m.is_dir():
                continue
            target = os.path.abspath(os.path.join(base, m.filename))
            if not target.startswith(base + os.sep):
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(m) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _find_ak3_kernel(ak3_dir: str) -> str | None:
    for name in ("Image.gz", "Image", "zImage-dtb", "zImage"):
        p = os.path.join(ak3_dir, name)
        if os.path.isfile(p):
            return p
    return None


def build_gki(
    boot_img: str,
    ak3_zip: str,
    out_dir: str,
    line_cb: Callable[[str], None] | None = None,
) -> str:
    """用 AK3 内的内核替换 boot.img 内核后重新打包为 GKI 镜像。"""
    if not is_boot_image(boot_img):
        raise PatchError("boot 镜像无效（缺少 ANDROID! 魔数）")
    magiskboot = _find_magiskboot()
    work = tempfile.mkdtemp(prefix="adbtool_gki_")
    try:
        ak3 = os.path.join(work, "ak3")
        _unpack_ak3(ak3_zip, ak3, line_cb=line_cb)
        kernel = _find_ak3_kernel(ak3)
        if not kernel:
            raise PatchError("AK3 中未找到内核文件（Image / Image.gz / zImage）")
        boot_dir = os.path.join(work, "boot")
        os.makedirs(boot_dir)
        if line_cb:
            line_cb("正在解包 boot 镜像...")
        subprocess.run(
            [magiskboot, "unpack", boot_img],
            cwd=boot_dir,
            capture_output=True,
            text=True,
            check=True,
            timeout=600,
        )
        shutil.copy(kernel, os.path.join(boot_dir, "kernel"))
        if line_cb:
            line_cb(f"已替换内核: {os.path.basename(kernel)}")
        subprocess.run(
            [magiskboot, "repack", boot_img],
            cwd=boot_dir,
            capture_output=True,
            text=True,
            check=True,
            timeout=600,
        )
        new_boot = os.path.join(boot_dir, "new-boot.img")
        if not os.path.isfile(new_boot):
            raise PatchError("打包后未找到 new-boot.img")
        os.makedirs(out_dir, exist_ok=True)
        base = os.path.splitext(os.path.basename(boot_img))[0]
        dst = os.path.join(out_dir, f"{base}-gki.img")
        shutil.copy(new_boot, dst)
        return dst
    finally:
        shutil.rmtree(work, ignore_errors=True)


def patch_skroot(
    boot_img: str,
    out_dir: str,
    line_cb: Callable[[str], None] | None = None,
) -> str:
    """SK-ROOT 一键修补。

    SK-ROOT 是厂商（三星 KNOX）私有的一键 Root 方案，需按具体机型/密钥做内核签名替换，
    通用实现无法保证正确，因此这里明确拒绝，避免生成损坏镜像。
    """
    raise PatchError(
        "SK-ROOT 为厂商（三星 KNOX）私有方案，需要按具体机型的密钥做内核签名替换，"
        "通用实现尚不支持，建议改用 Magisk 修补。"
    )
