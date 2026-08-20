from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
from typing import Callable


class FastbootError(Exception):
    pass


class FastbootClient:
    def __init__(self, fastboot_path: str | None = None):
        self.fastboot_path = fastboot_path or self._find_fastboot()
        self._device: str = ""

    @staticmethod
    def _find_fastboot() -> str:
        for env in ("ANDROID_FASTBOOT", "FASTBOOT"):
            p = os.environ.get(env)
            if p and os.path.isfile(p):
                return p
        for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
            root = os.environ.get(env)
            if root:
                cand = os.path.join(root, "platform-tools", "fastboot")
                if os.path.isfile(cand):
                    return cand
        cand = os.path.expanduser("~/Library/Android/sdk/platform-tools/fastboot")
        if os.path.isfile(cand):
            return cand
        which = shutil.which("fastboot")
        if which:
            return which
        raise FastbootError("未找到 fastboot，请安装 Android SDK platform-tools")

    @property
    def device(self) -> str:
        return self._device

    @device.setter
    def device(self, serial: str):
        self._device = serial

    def _base_cmd(self) -> list[str]:
        cmd = [self.fastboot_path]
        if self._device:
            cmd += ["-s", self._device]
        return cmd

    def _run(
        self,
        args: list[str],
        check: bool = True,
        timeout: int = 180,
        line_cb: Callable[[str], None] | None = None,
    ) -> str:
        cmd = self._base_cmd() + args
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        lines: list[str] = []
        try:
            while True:
                out = proc.stdout.readline()
                if not out:
                    if proc.poll() is not None:
                        break
                    continue
                line = out.rstrip("\n")
                lines.append(line)
                if line_cb:
                    line_cb(line)
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise FastbootError(f"fastboot 命令超时: {' '.join(args[:3])}")
        out = "\n".join(lines)
        if check and proc.returncode != 0:
            raise FastbootError(out.strip() or "fastboot 命令失败")
        return out

    def devices(self) -> list[str]:
        out = self._run(["devices"], check=False, timeout=30)
        serials: list[str] = []
        for line in out.splitlines():
            parts = line.split()
            if parts and len(parts) >= 2 and parts[1] == "fastboot":
                serials.append(parts[0])
        return serials

    def getvar(self, name: str) -> str:
        out = self._run(["getvar", name], check=False, timeout=60)
        m = re.search(rf"^\s*{re.escape(name)}:\s*(.*)$", out, re.MULTILINE)
        return m.group(1).strip() if m else ""

    def getvar_all(self, line_cb: Callable[[str], None] | None = None) -> dict[str, str]:
        out = self._run(["getvar", "all"], check=False, timeout=60, line_cb=line_cb)
        result: dict[str, str] = {}
        for line in out.splitlines():
            m = re.match(r"^\s*([\w.-]+):\s*(.*)$", line)
            if m:
                result[m.group(1)] = m.group(2).strip()
        return result

    def flash(self, partition: str, img_path: str, line_cb=None) -> None:
        self._run(["flash", partition, img_path], timeout=600, line_cb=line_cb)

    def erase(self, partition: str, line_cb=None) -> None:
        self._run(["erase", partition], timeout=300, line_cb=line_cb)

    def reboot(self, mode: str = "system", line_cb=None) -> None:
        args = ["reboot"]
        if mode and mode != "system":
            args.append(mode)
        self._run(args, check=False, timeout=60, line_cb=line_cb)

    def reboot_bootloader(self, line_cb=None) -> None:
        self.reboot("bootloader", line_cb)

    def continue_boot(self, line_cb=None) -> None:
        self._run(["continue"], check=False, timeout=60, line_cb=line_cb)

    def set_active(self, slot: str, line_cb=None) -> None:
        self._run(["--set-active", slot], check=False, timeout=60, line_cb=line_cb)

    def oem_unlock(self, line_cb=None) -> None:
        self._run(["oem", "unlock"], check=False, timeout=120, line_cb=line_cb)

    def flashing_unlock(self, line_cb=None) -> None:
        self._run(["flashing", "unlock"], check=False, timeout=120, line_cb=line_cb)

    def stage(self, file_path: str, line_cb=None) -> None:
        self._run(["stage", file_path], check=False, timeout=60, line_cb=line_cb)

    def oem_get_token(self) -> str:
        out = self._run(["oem", "get_token"], check=False, timeout=60)
        m = re.search(r"token:\s*(\S+)", out)
        return m.group(1) if m else ""

    _SCRIPT_OPS = {
        "flash",
        "erase",
        "update",
        "set_active",
        "--set-active",
        "oem",
        "flashing",
        "stage",
        "getvar",
    }

    def parse_flash_all(self, script_path: str) -> list[dict]:
        with open(script_path, "r", encoding="utf-8", errors="replace") as f:
            raw_lines = f.readlines()
        # 合并续行
        merged: list[str] = []
        pending = ""
        for rl in raw_lines:
            s = rl.rstrip("\n").rstrip()
            if not s.strip():
                continue
            if pending:
                pending = pending + " " + s.strip()
            else:
                pending = s.strip()
            if not s.endswith("\\"):
                merged.append(pending)
                pending = ""
        if pending:
            merged.append(pending)

        cmds: list[dict] = []
        for line in merged:
            if line.startswith("#") or line.startswith("::"):
                continue
            body = line
            # 去掉行内注释（-- 后的单引号包裹? 保守处理：去掉 `` 和 $() 段）
            body = re.sub(r"`[^`]*`", "", body)
            body = re.sub(r"\$\([^)]*\)", "", body)
            body = re.sub(r"\$([0-9]|\*)", "", body)
            if "|" in body or "if " in body[:8] or body.startswith(("echo", "exit", "read", "set ")):
                continue
            if not (body.startswith("fastboot") or body.startswith("adb")):
                continue
            parts = shlex.split(body)
            if not parts or parts[0] not in ("fastboot", "adb"):
                continue
            if parts[0] == "fastboot":
                op = next(
                    (t for t in parts[1:] if t in self._SCRIPT_OPS),
                    None,
                )
                if op is None:
                    continue
            cmds.append({"tool": parts[0], "args": parts[1:], "raw": line})
        return cmds

    def run_flash_all(
        self,
        script_path: str,
        base_dir: str,
        line_cb: Callable[[str], None] | None = None,
        progress_cb: Callable[[int, int, str], None] | None = None,
    ) -> None:
        cmds = self.parse_flash_all(script_path)
        total = len(cmds)
        for i, c in enumerate(cmds, 1):
            if progress_cb:
                progress_cb(i, total, c["raw"])
            if c["tool"] == "fastboot":
                args = []
                for a in c["args"]:
                    if a.endswith((".img", ".zip", ".dat")) and not os.path.isabs(a):
                        p = os.path.join(base_dir, a)
                        args.append(p if os.path.exists(p) else a)
                    else:
                        args.append(a)
                self._run(args, check=False, timeout=600, line_cb=line_cb)
            elif c["tool"] == "adb":
                subprocess.run([shutil.which("adb") or "adb"] + c["args"], timeout=300)

    @staticmethod
    def _find_zstd() -> str:
        which = shutil.which("zstd")
        if which:
            return which
        raise FastbootError("未找到 zstd，请用 brew install zstd 安装")

    def _find_flash_script(self, pkg_dir: str) -> str:
        names = (
            "flash_all.sh",
            "flash_all.bat",
            "flash-all.sh",
            "flash-all.bat",
            "FlashScript.sh",
            "FlashScript.bat",
        )
        for n in names:
            p = os.path.join(pkg_dir, n)
            if os.path.isfile(p):
                return p
        for f in os.listdir(pkg_dir):
            low = f.lower()
            if low.startswith("flash_all") and low.endswith((".sh", ".bat")):
                return os.path.join(pkg_dir, f)
        return ""

    _IMG_EXTS = (".img", ".elf", ".mbn", ".bin", ".fv", ".txt", ".dat", ".melf")

    def _resolve_script_arg(self, a: str, base: str, images_dir: str) -> str:
        if a.startswith("/images/") or a.startswith("images/"):
            cand = os.path.join(images_dir, os.path.basename(a))
            if os.path.isfile(cand):
                return cand
            return cand
        if os.path.isabs(a):
            return a
        for cand in (os.path.join(base, a), os.path.join(images_dir, a)):
            if os.path.isfile(cand):
                return cand
        return a

    def parse_package(self, pkg_dir: str) -> dict:
        rd = ""
        rd_path = os.path.join(pkg_dir, "bin", "right_device")
        if os.path.isfile(rd_path):
            with open(rd_path, "r", encoding="utf-8", errors="replace") as f:
                rd = f.read().strip()
        images_dir = os.path.join(pkg_dir, "images")
        script = self._find_flash_script(pkg_dir)
        if script:
            cmds = self.parse_flash_all(script)
            base = os.path.dirname(os.path.abspath(script))
            cleaned: list[dict] = []
            for c in cmds:
                if c["tool"] != "fastboot":
                    continue
                args = list(c["args"])
                if not args:
                    continue
                op = args[0]
                if op in ("reboot", "reboot-bootloader", "continue", "getvar"):
                    continue
                resolved = [args[0]]
                for a in args[1:]:
                    if a.endswith(self._IMG_EXTS) and (
                        not os.path.isabs(a) or a.startswith("/images/")
                    ):
                        resolved.append(self._resolve_script_arg(a, base, images_dir))
                    else:
                        resolved.append(a)
                cleaned.append(
                    {"tool": "fastboot", "args": resolved, "raw": c["raw"]}
                )
            if cleaned:
                ab = any("_ab" in a for c in cleaned for a in c["args"])
                return {"commands": cleaned, "right_device": rd, "ab": ab, "source": "script"}

        if not os.path.isdir(images_dir):
            raise FastbootError("刷机包目录里既没有 flash_all 脚本，也没有 images/ 目录")

        files = sorted(os.listdir(images_dir))
        ab = any(f.endswith("_ab.img") or f.endswith("_ab.img.zst") for f in files)
        parts: list[str] = []
        for f in files:
            if f.endswith(".img.zst"):
                base = f[: -len(".img.zst")]
            elif f.endswith(".img") and os.path.isfile(os.path.join(images_dir, f)):
                base = f[: -len(".img")]
            else:
                continue
            name = base[: -len("_ab")] if base.endswith("_ab") else base
            if name in ("super", "cust", "preloader_raw"):
                continue
            if name not in parts:
                parts.append(name)

        cmds: list[dict] = []
        for p in parts:
            if ab:
                src = os.path.join(images_dir, f"{p}_ab.img")
                if not os.path.isfile(src):
                    src = os.path.join(images_dir, f"{p}_ab.img.zst")
                cmds.append({"tool": "fastboot", "args": ["flash", f"{p}_a", src], "raw": f"flash {p}_a ← {os.path.basename(src)}"})
                cmds.append({"tool": "fastboot", "args": ["flash", f"{p}_b", src], "raw": f"flash {p}_b ← {os.path.basename(src)}"})
            else:
                src = os.path.join(images_dir, f"{p}.img")
                if not os.path.isfile(src):
                    src = os.path.join(images_dir, f"{p}.img.zst")
                cmds.append({"tool": "fastboot", "args": ["flash", p, src], "raw": f"flash {p} ← {os.path.basename(src)}"})
        for sp in ("cust", "super"):
            for ext in (".img", ".img.zst"):
                src = os.path.join(images_dir, f"{sp}{ext}")
                if os.path.isfile(src):
                    cmds.append({"tool": "fastboot", "args": ["flash", sp, src], "raw": f"flash {sp} ← {os.path.basename(src)}"})
                    break
        for ext in (".img", ".img.zst"):
            src = os.path.join(images_dir, f"preloader_raw{ext}")
            if os.path.isfile(src):
                for part in ("preloader_a", "preloader_b", "preloader1", "preloader2"):
                    cmds.append({"tool": "fastboot", "args": ["flash", part, src], "raw": f"flash {part} ← {os.path.basename(src)}"})
                break
        if ab:
            cmds.append({"tool": "fastboot", "args": ["--set-active", "a"], "raw": "set_active a"})
        return {"commands": cmds, "right_device": rd, "ab": ab, "source": "images"}

    def run_package(
        self,
        cmds: list[dict],
        line_cb: Callable[[str], None] | None = None,
        progress_cb: Callable[[int, int, str], None] | None = None,
    ) -> None:
        zstd = None
        tmp_root = tempfile.mkdtemp(prefix="adbtool_fbimg_")
        try:
            total = len(cmds)
            for i, c in enumerate(cmds, 1):
                if progress_cb:
                    progress_cb(i, total, c["raw"])
                resolved = []
                for a in c["args"]:
                    if a.endswith(".img.zst"):
                        plain = a[: -len(".zst")]
                        if os.path.isfile(plain):
                            resolved.append(plain)
                            continue
                        if zstd is None:
                            zstd = self._find_zstd()
                        dst = os.path.join(
                            tmp_root, os.path.basename(a)[: -len(".zst")]
                        )
                        if not os.path.isfile(dst):
                            subprocess.run(
                                [zstd, "-d", a, "-o", dst],
                                check=True,
                                timeout=3600,
                            )
                        resolved.append(dst)
                    else:
                        resolved.append(a)
                self._run(resolved, check=False, timeout=3600, line_cb=line_cb)
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)