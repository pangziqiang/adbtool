from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
from typing import Callable, Final


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

    def get_partition_list(self, line_cb=None):
        """Read device partition table. Returns list of partition names."""
        # Method 1: try 'getvar all' for partition-type entries
        all_vars = self.getvar_all(line_cb)
        partitions = set()
        for key in all_vars:
            m = re.match(r"partition-type:(.+)", key)
            if m:
                partitions.add(m.group(1))
            m2 = re.match(r"partition-size:(.+)", key)
            if m2:
                partitions.add(m2.group(1))
        if partitions:
            return sorted(partitions)

        # Method 2: probe common partition names
        common = [
            "boot",
            "system",
            "vendor",
            "product",
            "dtbo",
            "vbmeta",
            "recovery",
            "cache",
            "userdata",
            "metadata",
            "modem",
            "boot_b",
            "system_b",
            "vendor_b",
            "product_b",
            "dtbo_b",
            "vbmeta_b",
            "modem_b",
            "super",
            "init_boot",
            "init_boot_b",
            "vbmeta_system",
            "vbmeta_vendor",
            "logo",
            "abl",
            "xbl",
            "rpm",
            "tz",
            "devcfg",
            "keymaster",
            "misc",
            "persist",
            "frp",
            "config",
            "rawdump",
            "ddr",
            "sec",
        ]
        for name in common:
            try:
                v = self._run(["getvar", f"partition-type:{name}"], check=False, timeout=10)
                if "OKAY" in v and "empty" not in v.lower():
                    partitions.add(name)
            except Exception:
                pass
        return sorted(partitions)

    def flash(self, partition: str, img_path: str, line_cb=None) -> None:
        if img_path.endswith((".zst", ".lz4")):
            plain, tmp_root = self._decompress_to_tmp(img_path)
            try:
                self._run(["flash", partition, plain], timeout=600, line_cb=line_cb)
            finally:
                shutil.rmtree(tmp_root, ignore_errors=True)
        else:
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

    _SCRIPT_OPS: Final = {
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
            if line.startswith(("#", "::")):
                continue
            body = line
            # 去掉行内注释（-- 后的单引号包裹? 保守处理：去掉 `` 和 $() 段）
            body = re.sub(r"`[^`]*`", "", body)
            body = re.sub(r"\$\([^)]*\)", "", body)
            body = re.sub(r"\$([0-9]|\*)", "", body)
            if "|" in body or "if " in body[:8] or body.startswith(("echo", "exit", "read", "set ")):
                continue
            if not body.startswith(("fastboot", "adb")):
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
                subprocess.run([shutil.which("adb") or "adb"] + c["args"], timeout=300, check=False)

    @staticmethod
    def _find_zstd() -> str:
        for p in (
            shutil.which("zstd"),
            "/usr/local/bin/zstd",
            "/opt/homebrew/bin/zstd",
        ):
            if p and os.path.isfile(p):
                return p
        raise FastbootError("未找到 zstd，请用 brew install zstd 安装")

    @staticmethod
    def _find_lz4() -> str:
        for p in (
            shutil.which("lz4"),
            "/usr/local/bin/lz4",
            "/opt/homebrew/bin/lz4",
        ):
            if p and os.path.isfile(p):
                return p
        raise FastbootError("未找到 lz4，请用 brew install lz4 安装")

    def _decompress_to_tmp(self, path: str) -> tuple[str, str]:
        """将 .zst / .lz4 镜像解压到临时目录，返回 (明文路径, 临时目录)。"""
        tmp_root = tempfile.mkdtemp(prefix="adbtool_fbimg_")
        if path.endswith(".lz4"):
            dst = os.path.join(tmp_root, os.path.basename(path)[: -len(".lz4")])
            lz4 = self._find_lz4()
            subprocess.run([lz4, "-d", path, dst], check=True, timeout=3600)
        else:
            dst = os.path.join(tmp_root, os.path.basename(path)[: -len(".zst")])
            zstd = self._find_zstd()
            subprocess.run([zstd, "-d", path, "-o", dst], check=True, timeout=3600)
        return dst, tmp_root

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
        if a.startswith(("/images/", "images/")):
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

    def parse_package(self, pkg_dir: str, line_cb=None) -> dict:
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
                    if a.endswith(self._IMG_EXTS) and (not os.path.isabs(a) or a.startswith("/images/")):
                        resolved.append(self._resolve_script_arg(a, base, images_dir))
                    else:
                        resolved.append(a)
                cleaned.append({"tool": "fastboot", "args": resolved, "raw": c["raw"]})
            if cleaned:
                ab = any("_ab" in a for c in cleaned for a in c["args"])
                return {"commands": cleaned, "right_device": rd, "ab": ab, "source": "script"}

        payload = self._find_payload(pkg_dir)
        if payload:
            out = os.path.join(pkg_dir, ".payload_extracted")
            if not os.path.isdir(out) or not os.listdir(out):
                from .payload_dumper import extract

                if line_cb:
                    line_cb("正在解包 payload.bin（全量包较大，可能耗时数分钟）...")
                extract(payload, out, line_cb=line_cb)
            images_dir = out
        if not os.path.isdir(images_dir):
            raise FastbootError("刷机包目录里既没有 flash_all 脚本、payload.bin，也没有 images/ 目录")
        if not payload and self._looks_custom_kernel_ab(images_dir):
            return self._build_custom_ab_cmds(images_dir, rd)
        return self._build_images_cmds(images_dir, rd)

    @staticmethod
    def _find_payload(pkg_dir: str) -> str | None:
        for cand in (
            os.path.join(pkg_dir, "payload.bin"),
            os.path.join(pkg_dir, "images", "payload.bin"),
        ):
            if os.path.isfile(cand):
                return cand
        for root, _, files in os.walk(pkg_dir):
            for f in files:
                if f == "payload.bin":
                    return os.path.join(root, f)
        return None

    @staticmethod
    def _looks_custom_kernel_ab(images_dir: str) -> bool:
        """识别「第三方自定义 ROM」风格：images/ 子目录（任意层级）里有 boot.img / init_boot.img。
        这类包顶层镜像不带 _ab 后缀，但脚本实际刷到 <分区>_ab（当前槽），
        内核（boot/init_boot）由用户在 Root 与无 Root 版之间二选一。"""
        return bool(FastbootClient._find_kernel_images(images_dir))

    # 会被当作「可二选一内核」的镜像名（顶层同名文件是待刷分区镜像，不算）
    _KERNEL_IMG_NAMES = ("boot.img", "init_boot.img")
    # 路径关键字判定「无 Root」优先于「Root」（noroot 里也含 root 子串）
    _NOROOT_HINTS = (
        "noroot",
        "no_root",
        "no-root",
        "unroot",
        "stock",
        "official",
        "官方",
        "原厂",
        "无root",
        "无 root",
        "未root",
        "未 root",
    )
    _ROOT_HINTS = ("kernelsu", "sukisu", "ksu", "magisk", "root", "面具", "修补", "有root", "有 root")
    # 只写「kernel」的目录按惯例是无 Root 官方内核（优先级低于上面的 root 关键字，
    # 这样 Rootkernel 仍会被判成 Root）
    _NOROOT_ONLY_HINTS = ("kernel",)
    _KERNEL_SKIP_DIRS = frozenset({".git", ".svn", "__pycache__", "node_modules", ".payload_extracted"})

    @staticmethod
    def _find_kernel_images(images_dir: str, max_depth: int = 3) -> list[dict]:
        """遍历 images/ 下所有子目录，收集 boot.img / init_boot.img 内核镜像。"""
        if not os.path.isdir(images_dir):
            return []
        root = os.path.abspath(images_dir)
        found: list[dict] = []
        for cur, dirs, files in os.walk(root):
            rel_dir = os.path.relpath(cur, root)
            depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
            dirs[:] = [d for d in dirs if d not in FastbootClient._KERNEL_SKIP_DIRS and not d.startswith(".")]
            if depth >= max_depth:
                dirs[:] = []
            if depth == 0:
                continue  # 顶层是待刷分区镜像，不是二选一内核
            for f in files:
                if f.lower() in FastbootClient._KERNEL_IMG_NAMES:
                    found.append(
                        {
                            "path": os.path.join(cur, f),
                            "rel": os.path.join(rel_dir, f).replace(os.sep, "/"),
                            "img": f,
                            "dir": rel_dir.replace(os.sep, "/"),
                        }
                    )
        return sorted(found, key=lambda e: e["rel"].lower())

    @staticmethod
    def _classify_kernel(rel: str) -> str:
        """按路径关键字把内核镜像归类为 root / noroot / unknown。"""
        low = rel.replace("\\", "/").lower()
        if any(h in low for h in FastbootClient._NOROOT_HINTS):
            return "noroot"
        if any(h in low for h in FastbootClient._ROOT_HINTS):
            return "root"
        if any(h in low for h in FastbootClient._NOROOT_ONLY_HINTS):
            return "noroot"
        return "unknown"

    def _build_kernel_choices(self, images_dir: str) -> list[dict]:
        """把遍历到的内核镜像按 Root / 无 Root 归类成二选一选项；认不出则按目录名逐个列出。"""
        buckets: dict[str, list[dict]] = {"root": [], "noroot": [], "unknown": []}
        for e in self._find_kernel_images(images_dir):
            buckets[self._classify_kernel(e["rel"])].append(e)
        # 同类别里有多个时，优先选与顶层主内核镜像同名的那个
        top = {f.lower() for f in os.listdir(images_dir) if os.path.isfile(os.path.join(images_dir, f))}
        preferred = "init_boot.img" if "init_boot.img" in top else "boot.img"
        for items in buckets.values():
            items.sort(key=lambda e: (e["img"].lower() != preferred, e["rel"].lower()))

        def make(label: str, e: dict) -> dict:
            part = "init_boot_ab" if e["img"].lower().startswith("init_boot") else "boot_ab"
            return {"label": label, "path": e["path"], "dir": e["dir"], "part": part}

        choices: list[dict] = []
        for kind, label in (("root", "Root 内核（KernelSU）"), ("noroot", "无 Root 官方内核")):
            items = buckets[kind]
            if not items:
                continue
            e = items[0]
            choices.append(make(label if len(items) == 1 else f"{label} - {e['dir']}", e))
        for e in buckets["unknown"]:
            choices.append(make(f"内核：{e['rel']}", e))
        return choices

    def _build_custom_ab_cmds(self, images_dir: str, rd: str) -> dict:
        """第三方自定义 ROM：顶层槽位镜像刷到 <分区>_ab，super/system/vendor/cust 单分区；
        内核由遍历 images/ 子目录找到，用户在有 Root / 无 Root 之间二选一后刷到
        boot_ab / init_boot_ab（通过 boot_choices 交由 UI 处理）。"""
        files = sorted(os.listdir(images_dir))
        parts: list[str] = []
        for f in files:
            if f.endswith(".img.zst"):
                base = f[: -len(".img.zst")]
            elif f.endswith(".img") and os.path.isfile(os.path.join(images_dir, f)):
                base = f[: -len(".img")]
            else:
                continue
            name = base.removesuffix("_ab")
            if name in ("super", "system", "vendor", "cust", "preloader_raw"):
                continue
            if name not in parts:
                parts.append(name)

        def src(name: str) -> str | None:
            for ext in (".img", ".img.zst"):
                cand = os.path.join(images_dir, name + ext)
                if os.path.isfile(cand):
                    return cand
            return None

        cmds: list[dict] = []
        for p in parts:
            s = src(p)
            if not s:
                continue
            cmds.append(
                {
                    "tool": "fastboot",
                    "args": ["flash", f"{p}_ab", s],
                    "raw": f"flash {p}_ab ← {os.path.basename(s)}",
                }
            )
        for sp in ("system", "vendor", "cust", "super"):
            s = src(sp)
            if s:
                cmds.append(
                    {
                        "tool": "fastboot",
                        "args": ["flash", sp, s],
                        "raw": f"flash {sp} ← {os.path.basename(s)}",
                    }
                )
        for ext in (".img", ".img.zst"):
            s = os.path.join(images_dir, f"preloader_raw{ext}")
            if os.path.isfile(s):
                for part in ("preloader_a", "preloader_b", "preloader1", "preloader2"):
                    cmds.append(
                        {
                            "tool": "fastboot",
                            "args": ["flash", part, s],
                            "raw": f"flash {part} ← {os.path.basename(s)}",
                        }
                    )
                break
        boot_choices = self._build_kernel_choices(images_dir)
        return {
            "commands": cmds,
            "right_device": rd,
            "ab": True,
            "source": "custom_ab",
            "boot_choices": boot_choices,
        }

    def _build_images_cmds(self, images_dir: str, rd: str) -> dict:
        files = sorted(os.listdir(images_dir))
        ab = any(f.endswith(("_ab.img", "_ab.img.zst")) for f in files)
        parts: list[str] = []
        for f in files:
            if f.endswith(".img.zst"):
                base = f[: -len(".img.zst")]
            elif f.endswith(".img") and os.path.isfile(os.path.join(images_dir, f)):
                base = f[: -len(".img")]
            else:
                continue
            name = base.removesuffix("_ab")
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
                cmds.append(
                    {
                        "tool": "fastboot",
                        "args": ["flash", f"{p}_a", src],
                        "raw": f"flash {p}_a ← {os.path.basename(src)}",
                    }
                )
                cmds.append(
                    {
                        "tool": "fastboot",
                        "args": ["flash", f"{p}_b", src],
                        "raw": f"flash {p}_b ← {os.path.basename(src)}",
                    }
                )
            else:
                src = os.path.join(images_dir, f"{p}.img")
                if not os.path.isfile(src):
                    src = os.path.join(images_dir, f"{p}.img.zst")
                cmds.append(
                    {"tool": "fastboot", "args": ["flash", p, src], "raw": f"flash {p} ← {os.path.basename(src)}"}
                )
        for sp in ("cust", "super"):
            for ext in (".img", ".img.zst"):
                src = os.path.join(images_dir, f"{sp}{ext}")
                if os.path.isfile(src):
                    cmds.append(
                        {"tool": "fastboot", "args": ["flash", sp, src], "raw": f"flash {sp} ← {os.path.basename(src)}"}
                    )
                    break
        for ext in (".img", ".img.zst"):
            src = os.path.join(images_dir, f"preloader_raw{ext}")
            if os.path.isfile(src):
                for part in ("preloader_a", "preloader_b", "preloader1", "preloader2"):
                    cmds.append(
                        {
                            "tool": "fastboot",
                            "args": ["flash", part, src],
                            "raw": f"flash {part} ← {os.path.basename(src)}",
                        }
                    )
                break
        if ab:
            cmds.append({"tool": "fastboot", "args": ["--set-active", "a"], "raw": "set_active a"})
        return {"commands": cmds, "right_device": rd, "ab": ab, "source": "images"}

    def parse_payload(self, payload_path: str, line_cb=None) -> dict:
        from .payload_dumper import is_payload

        if not is_payload(payload_path):
            raise FastbootError("不是有效的 payload.bin 文件")
        out = os.path.join(os.path.dirname(os.path.abspath(payload_path)), ".payload_extracted")
        if not os.path.isdir(out) or not os.listdir(out):
            from .payload_dumper import extract

            if line_cb:
                line_cb("正在解包 payload.bin（全量包较大，可能耗时数分钟）...")
            extract(payload_path, out, line_cb=line_cb)
        return self._build_images_cmds(out, "")

    def parse_zip(self, zip_path: str, line_cb=None) -> dict:
        import zipfile

        name = os.path.basename(zip_path)
        out = os.path.join(
            os.path.dirname(os.path.abspath(zip_path)),
            "." + os.path.splitext(name)[0] + "_extracted",
        )
        base = os.path.abspath(out)
        if not os.path.isdir(out) or not os.listdir(out):
            os.makedirs(out, exist_ok=True)
            with zipfile.ZipFile(zip_path) as zf:
                infos = zf.infolist()
                if len(infos) > 5000:
                    raise FastbootError("zip 条目过多，已中止解压")
                total = sum(m.file_size for m in infos)
                if total > 60 * 1024**3:
                    raise FastbootError("zip 解压总大小超过 60GB，已中止解压")
                for m in infos:
                    if m.is_dir():
                        continue
                    target = os.path.abspath(os.path.join(base, m.filename))
                    if not target.startswith(base + os.sep):
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(m) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        return self.parse_package(out, line_cb=line_cb)

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
                        dst = os.path.join(tmp_root, os.path.basename(a)[: -len(".zst")])
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
