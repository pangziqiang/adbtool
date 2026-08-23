from __future__ import annotations

import os
import pty
import re
import select
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath


class AdbError(Exception):
    pass


@dataclass
class FileEntry:
    name: str
    path: str
    is_dir: bool
    size: int = 0
    mtime: str = ""


@dataclass
class DeviceInfo:
    serial: str
    model: str = ""
    android_version: str = ""
    state: str = ""
    market_name: str = ""
    code: str = ""
    manufacturer: str = ""
    android_sdk: str = ""
    resolution: str = ""
    battery_level: str = ""
    battery_status: str = ""
    storage_total: str = ""
    storage_free: str = ""
    security_patch: str = ""
    miui_version: str = ""



class AdbClient:
    def __init__(self, adb_path: str | None = None):
        self.adb_path = adb_path or self._find_adb()
        self._device: str = ""

    @staticmethod
    def _find_adb() -> str:
        for env in ("ANDROID_ADB", "ADB"):
            p = os.environ.get(env)
            if p and os.path.isfile(p):
                return p
        for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
            root = os.environ.get(env)
            if root:
                cand = os.path.join(root, "platform-tools", "adb")
                if os.path.isfile(cand):
                    return cand
        cand = os.path.expanduser("~/Library/Android/sdk/platform-tools/adb")
        if os.path.isfile(cand):
            return cand
        which = shutil.which("adb")
        if which:
            return which
        raise AdbError("未找到 adb，请安装 Android SDK platform-tools")

    @property
    def device(self) -> str:
        return self._device

    @device.setter
    def device(self, serial: str):
        self._device = serial

    def _run(self, args: list[str], check: bool = True, timeout: int = 120) -> str:
        cmd = [self.adb_path]
        if self._device:
            cmd += ["-s", self._device]
        cmd += args
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            raise AdbError(f"adb 命令超时: {' '.join(cmd[1:3])}") from None
        if check and proc.returncode != 0:
            raise AdbError(proc.stderr.strip() or proc.stdout.strip())
        return proc.stdout

    def list_devices(self) -> list[DeviceInfo]:
        out = self._run(["devices", "-l"], check=False)
        devices: list[DeviceInfo] = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if not parts:
                continue
            serial = parts[0]
            state = parts[1] if len(parts) > 1 else ""
            info = DeviceInfo(serial=serial, state=state)
            for tok in parts[2:]:
                if tok.startswith("model:"):
                    info.model = tok.split(":", 1)[1]
                elif tok.startswith("device:"):
                    info.android_version = tok.split(":", 1)[1]
            devices.append(info)
        return devices

    def list_dir(self, path: str) -> list[FileEntry]:
        target = path if path == "/" else path.rstrip("/") + "/"
        quoted = shlex.quote(target)
        out = self._run(["shell", f"ls -la {quoted}"])
        entries: list[FileEntry] = []
        for line in out.splitlines():
            m = re.match(
                r"^([\-dlbcps][rwxsStT\-]{9})\s+\d+\s+\S+\s+\S+\s+(\d+)\s+"
                r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}|[A-Za-z]{3}\s+\d+\s+"
                r"(?:\d{4}|\d{2}:\d{2}))\s+(.+)$",
                line,
            )
            if not m:
                continue
            kind, size, mtime, raw_name = m.groups()
            name = raw_name.split(" -> ", 1)[0].strip()
            if name in (".", ".."):
                continue
            entries.append(
                FileEntry(
                    name=name,
                    path=str(PurePosixPath(path, name)),
                    is_dir=(kind[0] == "d"),
                    size=int(size),
                    mtime=mtime,
                )
            )
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    def push(self, local_path: str, remote_path: str, callback=None) -> None:
        self._run_transfer(["push", "-p", local_path, remote_path])
        if callback:
            callback(1.0)

    def pull(self, remote_path: str, local_path: str, callback=None) -> None:
        self._run_transfer(["pull", "-p", remote_path, local_path])
        if callback:
            callback(1.0)

    def _run_transfer(
        self,
        args: list[str],
        idle_timeout: float = 180,
        total_timeout: float = 21600,
    ) -> str:
        """运行传输命令，使用空闲超时：仅当长时间无数据传输时才中断。

        adb pull/push 对无线传输大文件较慢，用绝对超时容易误杀；这里改为
        用伪终端(pty)运行 adb，让 adb 把 stdout 当作终端并持续输出传输进度，
        再以 select 监听，只要有进度数据到达就刷新计时；仅当长时间无任何
        进度时才判定为连接卡死并中断。
        """
        cmd = [self.adb_path]
        if self._device:
            cmd += ["-s", self._device]
        cmd += args
        master, slave = pty.openpty()
        proc = subprocess.Popen(
            cmd, stdout=slave, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, close_fds=True,
        )
        os.close(slave)
        buf = b""
        last = time.time()
        start = time.time()
        try:
            while True:
                if proc.poll() is not None:
                    break
                if time.time() - start > total_timeout:
                    proc.kill()
                    raise AdbError("adb 传输超过总时长上限，已终止")
                wait = idle_timeout - (time.time() - last)
                if wait <= 0:
                    proc.kill()
                    raise AdbError(
                        f"adb 传输无响应（{int(idle_timeout)} 秒无数据），"
                        "无线连接可能不稳定"
                    )
                ready, _, _ = select.select([master], [], [], wait)
                if master in ready:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        data = b""
                    if not data:
                        break
                    buf += data
                    last = time.time()
            out = buf.decode(errors="replace")
            proc.wait()
            os.close(master)
            if proc.returncode != 0:
                raise AdbError(out.strip() or "adb 传输失败")
            return out
        except subprocess.TimeoutExpired:
            proc.kill()
            os.close(master)
            raise AdbError("adb 传输超时") from None

    def install_apk(self, apks: list[str], callback=None) -> None:
        for apk in apks:
            self._run(["install", "-r", apk], timeout=600)
        if callback:
            callback(1.0)

    def mkdir(self, remote_path: str) -> None:
        quoted = shlex.quote(remote_path)
        self._run(["shell", f"mkdir -p {quoted}"])

    def remove(self, remote_path: str, is_dir: bool = False) -> None:
        quoted = shlex.quote(remote_path)
        if is_dir:
            self._run(["shell", f"rm -rf {quoted}"])
        else:
            self._run(["shell", f"rm -f {quoted}"])

    def move(self, src: str, dst: str) -> None:
        s, d = shlex.quote(src), shlex.quote(dst)
        self._run(["shell", f"mv {s} {d}"])

    def copy(self, src: str, dst: str) -> None:
        s, d = shlex.quote(src), shlex.quote(dst)
        self._run(["shell", f"cp -r {s} {d}"])

    def get_storage_root(self) -> str:
        out = self._run(["shell", "echo $EXTERNAL_STORAGE"], check=False, timeout=6).strip()
        if out and out != "$EXTERNAL_STORAGE":
            return out
        for candidate in ("/sdcard", "/storage/emulated/0"):
            try:
                self._run(["shell", f"test -d {candidate} && echo ok"], timeout=6)
                return candidate
            except AdbError:
                continue
        raise AdbError("无法确定手机存储路径")

    def get_device_info(self) -> DeviceInfo:
        info = DeviceInfo(serial=self._device)
        prop_map = [
            ("model", "ro.product.model"),
            ("android_version", "ro.build.version.release"),
            ("market_name", "ro.product.marketname"),
            ("code", "ro.product.device"),
            ("manufacturer", "ro.product.manufacturer"),
            ("android_sdk", "ro.build.version.sdk"),
            ("security_patch", "ro.build.version.security_patch"),
            ("miui_version", "ro.miui.ui.version.name"),
        ]
        for field, prop in prop_map:
            try:
                value = self._run(["shell", f"getprop {prop}"], timeout=6).strip()
                if value:
                    setattr(info, field, value)
            except AdbError:
                pass
        try:
            out = self._run(["shell", "wm size"], check=False, timeout=6).strip()
            m = re.search(r"Physical size: (.+)", out)
            if m:
                info.resolution = m.group(1).strip()
        except AdbError:
            pass
        try:
            out = self._run(["shell", "dumpsys battery"], check=False, timeout=6)
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("level:"):
                    info.battery_level = line.split(":", 1)[1].strip() + "%"
                elif line.startswith("status:"):
                    code = line.split(":", 1)[1].strip()
                    status_map = {"2": "充电中", "3": "放电中", "4": "未充电", "5": "已充满"}
                    info.battery_status = status_map.get(code, code)
        except AdbError:
            pass
        try:
            out = self._run(["shell", "df /sdcard"], check=False, timeout=6)
            for line in out.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 4:
                    total_kb = int(parts[1]) if parts[1].isdigit() else 0
                    free_kb = int(parts[3]) if parts[3].isdigit() else 0
                    if total_kb > 0:
                        info.storage_total = self._fmt_kb(total_kb)
                        info.storage_free = self._fmt_kb(free_kb)
                        break
        except (AdbError, ValueError):
            pass
        return info

    @staticmethod
    def _fmt_kb(kb: int) -> str:
        gb = kb / 1048576
        if gb >= 1:
            return f"{gb:.1f} GB"
        return f"{kb / 1024:.0f} MB"

    # ---------- wireless pairing ----------

    def pair_wireless(self, host_port: str, code: str) -> str:
        proc = subprocess.run(
            [self.adb_path, "pair", host_port, code],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            raise AdbError(proc.stderr.strip() or proc.stdout.strip())
        return proc.stdout.strip()

    def connect_wireless(self, host_port: str) -> str:
        proc = subprocess.run(
            [self.adb_path, "connect", host_port],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            raise AdbError(proc.stderr.strip() or proc.stdout.strip())
        out = proc.stdout.strip()
        if "connected" not in out.lower():
            raise AdbError(out)
        return out

    # ---------- packages ----------

    def list_packages(
        self, third_party_only: bool = True, system_only: bool = False
    ) -> list[str]:
        if system_only:
            flag = " -s"
        elif third_party_only:
            flag = " -3"
        else:
            flag = ""
        out = self._run(["shell", f"pm list packages{flag}".strip()])
        return [
            line.split("package:", 1)[-1].strip()
            for line in out.splitlines()
            if line.startswith("package:")
        ]

    def list_package_paths(self, system_only: bool = False) -> dict[str, str]:
        flag = " -s" if system_only else " -3"
        out = self._run(["shell", f"pm list packages -f{flag}".strip()])
        mapping: dict[str, str] = {}
        for line in out.splitlines():
            if not line.startswith("package:"):
                continue
            body = line[len("package:"):]
            path, _, pkg = body.rpartition("=")
            if pkg and path:
                mapping[pkg] = path
        return mapping

    def uninstall(self, pkg: str) -> None:
        self._run(["uninstall", pkg], timeout=120)

    def start_app(self, pkg: str) -> None:
        quoted = shlex.quote(pkg)
        self._run(
            ["shell", f"monkey -p {quoted} -c android.intent.category.LAUNCHER 1"],
            timeout=60,
        )

    def force_stop(self, pkg: str) -> None:
        self._run(["shell", f"am force-stop {shlex.quote(pkg)}"])

    def clear_app(self, pkg: str) -> None:
        self._run(["shell", f"pm clear {shlex.quote(pkg)}"])

    # ---------- screenshot / recording ----------

    def screenshot(self, save_path: str) -> None:
        cmd = [self.adb_path]
        if self._device:
            cmd += ["-s", self._device]
        cmd += ["exec-out", "screencap", "-p"]
        with open(save_path, "wb") as f:
            proc = subprocess.run(cmd, stdout=f, timeout=60)
        if proc.returncode != 0:
            raise AdbError("截屏失败")

    def start_recording(self, remote_path: str = "/sdcard/adbtool_rec.mp4") -> str:
        cmd = [self.adb_path]
        if self._device:
            cmd += ["-s", self._device]
        cmd += ["shell", "screenrecord", "--time-limit", "1800", remote_path]
        self._rec_proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return remote_path

    def stop_recording(self) -> None:
        proc = getattr(self, "_rec_proc", None)
        if not proc:
            return
        try:
            out = self._run(["shell", "pidof screenrecord"], check=False).strip()
            pid = out.split()[0] if out else ""
            if pid:
                self._run(["shell", f"kill -2 {pid}"], check=False)
        except AdbError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()
        self._rec_proc = None

    # ---------- system toggles ----------

    def set_wifi(self, on: bool) -> None:
        self._run(["shell", f"svc wifi {'enable' if on else 'disable'}"])
        self._run(
            ["shell", f"settings put global wifi_on {1 if on else 0}"],
            check=False,
        )

    def _has_su(self) -> bool:
        key = self._device or ""
        cached = getattr(self, "_su_cache", {})
        if key in cached:
            return cached[key]
        out = self._run(["shell", "su -c id"], check=False)
        cached[key] = "uid=0" in out
        self._su_cache = cached
        return cached[key]

    def set_bluetooth(self, on: bool) -> None:
        action = "enable" if on else "disable"
        shell_cmd = (
            f'su -c "cmd bluetooth_manager {action}"'
            if self._has_su()
            else f"cmd bluetooth_manager {action}"
        )
        cmd = [self.adb_path]
        if self._device:
            cmd += ["-s", self._device]
        cmd += ["shell", shell_cmd]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        combined = proc.stdout + proc.stderr
        if "SecurityException" in combined or "Exception occurred" in combined:
            raise AdbError("设备限制了 shell 控制蓝牙，请在手机上操作")
        self._run(
            ["shell", f"settings put global bluetooth_on {1 if on else 0}"],
            check=False,
        )

    def get_wifi_state(self) -> bool:
        out = self._run(["shell", "dumpsys wifi"], check=False)
        for line in out.splitlines():
            if "Wi-Fi is" in line:
                return "enabled" in line
        return False

    def get_bluetooth_state(self) -> bool:
        out = self._run(
            ["shell", "dumpsys bluetooth_manager"], check=False
        )
        for line in out.splitlines():
            if "State:" in line:
                return "ON" in line.split("State:", 1)[1].upper()
        return False

    def get_stay_awake(self) -> bool:
        out = self._run(
            ["shell", "settings get global stay_on_while_plugged_in"],
            check=False,
        ).strip()
        return out.isdigit() and int(out) > 0

    def get_anim_scale(self) -> float:
        out = self._run(
            ["shell", "settings get global window_animation_scale"],
            check=False,
        ).strip()
        try:
            return float(out)
        except ValueError:
            return 1.0

    def set_stay_awake(self, on: bool) -> None:
        self._run(["shell", f"svc power stayon {'true' if on else 'false'}"])
        self._run(
            [
                "shell",
                f"settings put global stay_on_while_plugged_in {3 if on else 0}",
            ],
            check=False,
        )

    def set_anim_scale(self, scale: float) -> None:
        for key in (
            "window_animation_scale",
            "transition_animation_scale",
            "animator_duration_scale",
        ):
            self._run(
                ["shell", f"settings put global {key} {scale}"], check=False
            )

    # ---------- reboot ----------

    def reboot(self, mode: str = "system") -> None:
        args = ["reboot"]
        if mode and mode != "system":
            args.append(mode)
        self._run(args, check=False, timeout=60)
