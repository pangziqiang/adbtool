from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Final
from urllib.parse import urlparse

from PyQt6.QtCore import QPointF, QSize, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPixmap,
    QStandardItem,
    QStandardItemModel,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import offline_patch, payload_dumper
from .adb_client import AdbClient, AdbError
from .fastboot_client import FastbootClient, FastbootError

_SAVE_DIR = os.path.expanduser("~/Desktop")


class AdbTask(QThread):
    done = pyqtSignal(object)
    fail = pyqtSignal(str)

    def __init__(self, fn, args=(), parent=None):
        super().__init__(parent)
        self.fn = fn
        self.args = args

    def run(self):
        try:
            self.done.emit(self.fn(*self.args))
        except AdbError as e:
            self.fail.emit(str(e))
        except Exception as e:
            self.fail.emit(str(e))


class TaskList(QThread):
    done = pyqtSignal(str)
    fail = pyqtSignal(str)

    def __init__(self, fns, parent=None):
        super().__init__(parent)
        self.fns = fns

    def run(self):
        try:
            for fn in self.fns:
                fn()
            self.done.emit("ok")
        except AdbError as e:
            self.fail.emit(str(e))
        except Exception as e:
            self.fail.emit(str(e))


# ---------- wireless pairing ----------


class PairDialog(QDialog):
    paired = pyqtSignal()

    def __init__(self, adb: AdbClient, parent=None):
        super().__init__(parent)
        self.adb = adb
        self.setWindowTitle("无线 ADB 配对")
        self.setMinimumWidth(360)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.ip_edit = QLineEdit(self)
        self.ip_edit.setPlaceholderText("192.168.1.14:39999")
        self.code_edit = QLineEdit(self)
        self.code_edit.setPlaceholderText("6 位配对码")
        form.addRow("地址:端口", self.ip_edit)
        form.addRow("配对码", self.code_edit)
        lay.addLayout(form)
        hint = QLabel(
            "手机: 开发者选项 → 无线调试 → 使用配对码配对设备\n填入弹出的 IP:端口 和 6 位配对码后点“配对”",
            self,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666;")
        lay.addWidget(hint)
        self.status = QLabel("", self)
        lay.addWidget(self.status)
        btns = QDialogButtonBox(self)
        self.ok_btn = btns.addButton("配对", QDialogButtonBox.ButtonRole.AcceptRole)
        btns.addButton(QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._pair)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _pair(self):
        host = self.ip_edit.text().strip()
        code = self.code_edit.text().strip()
        if not host or not code:
            self.status.setText("请填写地址和配对码")
            return
        self.ok_btn.setEnabled(False)
        self.status.setText("正在配对...")
        try:
            self.adb.pair_wireless(host, code)
            self.adb.connect_wireless(host.split(":")[0] + ":5555")
        except AdbError as e:
            self.status.setText(f"失败: {e}")
            self.ok_btn.setEnabled(True)
            return
        self.status.setText("配对成功")
        self.paired.emit()
        self.accept()


# ---------- package manager ----------


_CACHE_DIR = os.path.join(os.path.expanduser("~/Library/Application Support"), "adbtool", "pkg_cache")
_APK_SIZE_LIMIT = 200 * 1024 * 1024


def _find_aapt() -> str | None:
    base = os.path.expanduser("~/Library/Android/sdk/build-tools")
    if os.path.isdir(base):
        for ver in sorted(os.listdir(base), reverse=True):
            p = os.path.join(base, ver, "aapt")
            if os.path.isfile(p):
                return p
    return None


def _parse_apk(aapt: str, apk: str) -> tuple[str, bytes]:
    label = ""
    icons: list[tuple[int, str]] = []
    try:
        proc = subprocess.run(
            [aapt, "dump", "badging", apk],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        for line in proc.stdout.splitlines():
            if line.startswith("application-label:") and not label:
                label = line.split(":", 1)[1].strip().strip("'")
            elif line.startswith("application-icon-"):
                rest = line[len("application-icon-") :]
                dpi, _, path = rest.partition(":")
                if path.strip().strip("'"):
                    icons.append((int(dpi), path.strip().strip("'")))
        with zipfile.ZipFile(apk) as z:
            for _, path in sorted(icons):
                try:
                    data = z.read(path)
                    if data[:4] == b"\x89PNG":
                        return label, data
                except KeyError:
                    continue
            names = [
                n
                for n in z.namelist()
                if n.lower().endswith(".png") and ("ic_launcher" in n.lower() or "/mipmap" in n.lower())
            ]
            for n in sorted(names):
                try:
                    data = z.read(n)
                    if data[:4] == b"\x89PNG":
                        return label, data
                except KeyError:
                    continue
            best = (0, b"")
            for n in z.namelist():
                low = n.lower()
                if not low.endswith((".png", ".webp")) or n.endswith(".9.png"):
                    continue
                try:
                    data = z.read(n)
                    if (data[:4] == b"\x89PNG" or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")) and len(data) > best[
                        0
                    ]:
                        best = (len(data), data)
                except Exception:
                    continue
            if best[1]:
                return label, best[1]
    except Exception:
        pass
    return label, b""


class EnrichWorker(QThread):
    updated = pyqtSignal(str, str, bytes)
    progress = pyqtSignal(int, int)
    finished = pyqtSignal()

    def __init__(self, adb: AdbClient, packages: list[str], parent=None, system_only: bool = False):
        super().__init__(parent)
        self.adb = adb
        self.packages = packages
        self.system_only = system_only
        self._stop = False
        self._adb_lock = threading.Lock()

    def _cache_dir(self) -> str:
        safe = re.sub(r"[^\w.-]", "_", self.adb.device)
        return os.path.join(_CACHE_DIR, safe)

    def run(self):
        aapt = _find_aapt()
        if not aapt:
            for p in self.packages:
                self.updated.emit(p, "", b"")
            return
        paths = self.adb.list_package_paths(system_only=self.system_only)
        cdir = self._cache_dir()
        os.makedirs(cdir, exist_ok=True)
        total = len(self.packages)
        tmpdir = tempfile.mkdtemp(prefix="adbtool_apk_")
        try:
            nw = min(4, max(1, total))
            done = 0
            with ThreadPoolExecutor(max_workers=nw) as ex:
                futures = {
                    ex.submit(self._process_one, pkg, aapt, paths.get(pkg, ""), tmpdir, cdir): pkg
                    for pkg in self.packages
                }
                for fut in as_completed(futures):
                    pkg = futures[fut]
                    try:
                        label, icn = fut.result()
                    except Exception:
                        label, icn = pkg, b""
                    done += 1
                    self.updated.emit(pkg, label, icn)
                    self.progress.emit(done, total)
                    if self._stop:
                        break
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            self.finished.emit()

    def _process_one(
        self,
        pkg: str,
        aapt: str,
        apk: str,
        tmpdir: str,
        cdir: str,
    ) -> tuple[str, bytes]:
        label_f = os.path.join(cdir, pkg + ".txt")
        icon_f = os.path.join(cdir, pkg + ".png")
        if os.path.isfile(label_f):
            try:
                with open(label_f, encoding="utf-8") as f:
                    label = f.read().strip()
            except OSError:
                label = ""
            icn = b""
            if os.path.isfile(icon_f):
                try:
                    with open(icon_f, "rb") as f:
                        icn = f.read()
                except OSError:
                    pass
            if label and label != pkg:
                return label, icn
        label, icn = pkg, b""
        if apk:
            try:
                with self._adb_lock:
                    size_s = self.adb._run(
                        ["shell", f"stat -c %s {shlex.quote(apk)}"],
                        check=False,
                    ).strip()
                if size_s.isdigit() and int(size_s) > _APK_SIZE_LIMIT:
                    apk = ""
            except Exception:
                pass
        if apk:
            tmp = os.path.join(tmpdir, hashlib.md5(pkg.encode()).hexdigest() + ".apk")
            try:
                with self._adb_lock:
                    self.adb._run_transfer(["pull", apk, tmp])
                label, icn = _parse_apk(aapt, tmp)
            except Exception:
                pass
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        if not label:
            label = pkg
        try:
            with open(label_f, "w", encoding="utf-8") as f:
                f.write(label)
            if icn:
                with open(icon_f, "wb") as f:
                    f.write(icn)
        except OSError:
            pass
        return label, icn


class PackageDialog(QDialog):
    def __init__(self, adb: AdbClient, parent=None):
        super().__init__(parent)
        self.adb = adb
        self.setWindowTitle("应用管理")
        self.resize(540, 560)
        self._task: AdbTask | None = None
        self._enrich: EnrichWorker | None = None
        self._enrich_sys: EnrichWorker | None = None
        self._sys_enriched = False
        self._items: dict[str, QListWidgetItem] = {}
        self._list_pkgs: dict[QListWidget, set[str]] = {}

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("搜索应用名/包名...")
        self.search.textChanged.connect(self._apply_filter)
        self.reload_btn = QPushButton("刷新", self)
        self.reload_btn.clicked.connect(self._load)
        top.addWidget(self.search, 1)
        top.addWidget(self.reload_btn)
        lay.addLayout(top)

        self.tabs = QTabWidget(self)
        self.user_list = self._make_list()
        self.sys_list = self._make_list()
        self.tabs.addTab(self.user_list, "用户软件")
        self.tabs.addTab(self.sys_list, "系统软件")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        lay.addWidget(self.tabs, 1)

        btn_row = QHBoxLayout()
        for text, slot in (
            ("卸载", self._uninstall),
            ("启动", self._start),
            ("停止", self._stop),
            ("清数据", self._clear),
        ):
            b = QPushButton(text, self)
            b.clicked.connect(slot)
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        self.close_btn = QPushButton("关闭", self)
        self.close_btn.clicked.connect(self.accept)
        btn_row.addWidget(self.close_btn)
        lay.addLayout(btn_row)

        self.status = QLabel("", self)
        lay.addWidget(self.status)
        self._load()

    def _make_list(self) -> QListWidget:
        lw = QListWidget(self)
        lw.setSpacing(3)
        lw.setStyleSheet("QListWidget::item { padding: 4px; }")
        return lw

    def _load(self):
        self.status.setText("正在读取应用列表...")
        self._run_task(self._fetch_lists, (), self._on_lists)

    def _fetch_lists(self):
        return (
            self.adb.list_packages(third_party_only=True),
            self.adb.list_packages(system_only=True),
        )

    def _on_lists(self, lists):
        user, sys = lists
        self._items = {}
        self._list_pkgs = {self.user_list: set(user), self.sys_list: set(sys)}
        self._fill(self.user_list, user)
        self._fill(self.sys_list, sys)
        self._apply_filter()
        self._enrich = EnrichWorker(self.adb, sorted(user), self)
        self._enrich.updated.connect(self._on_enrich)
        self._enrich.progress.connect(lambda i, t: self.status.setText(f"正在加载应用信息 {i}/{t}..."))
        self._enrich.finished.connect(self._on_enrich_done)
        self._enrich.start()

    def _on_tab_changed(self, idx: int):
        if idx == 1 and not self._sys_enriched:
            self._sys_enriched = True
            pkgs = sorted(self._list_pkgs.get(self.sys_list, ()))
            self._enrich_sys = EnrichWorker(self.adb, pkgs, self, system_only=True)
            self._enrich_sys.updated.connect(self._on_enrich)
            self._enrich_sys.progress.connect(lambda i, t: self.status.setText(f"正在加载系统应用信息 {i}/{t}..."))
            self._enrich_sys.finished.connect(self._on_enrich_done)
            self._enrich_sys.start()

    def _on_enrich_done(self):
        others = [
            w for w in (self._enrich, self._enrich_sys) if w is not None and w is not self.sender() and w.isRunning()
        ]
        if not others:
            self.status.setText("应用信息加载完成")

    def _fill(self, listw: QListWidget, packages: list[str]):
        listw.clear()
        for p in sorted(packages):
            item = QListWidgetItem(p)
            item.setSizeHint(QSize(0, 46))
            listw.addItem(item)
            self._items[p] = item

    def _on_enrich(self, pkg: str, label: str, icon_bytes: bytes):
        item = self._items.get(pkg)
        if not item:
            return
        item.setText(f"{label}\n{pkg}")
        if icon_bytes:
            pm = QPixmap()
            if pm.loadFromData(icon_bytes):
                item.setIcon(
                    QIcon(
                        pm.scaled(
                            28,
                            28,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                    )
                )
                return
        item.setIcon(QApplication.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon))

    def _apply_filter(self, *_):
        q = self.search.text().strip().lower()
        for listw, pkgs in self._list_pkgs.items():
            shown = 0
            for p in sorted(pkgs):
                item = self._items.get(p)
                if not item:
                    continue
                visible = not q or q in p.lower() or q in item.text().lower()
                item.setHidden(not visible)
                if visible:
                    shown += 1
            if listw is self.user_list:
                self._user_shown = shown
            else:
                self._sys_shown = shown
        self.status.setText(
            f"用户软件 {self._user_shown}/{len(self._list_pkgs[self.user_list])}，"
            f"系统软件 {self._sys_shown}/{len(self._list_pkgs[self.sys_list])}"
        )

    def _current(self) -> str:
        item = self.tabs.currentWidget().currentItem()
        if not item:
            self.status.setText("请先选中一个应用")
            return ""
        return item.text().split("\n")[-1]

    def _run_task(self, fn, args, on_done):
        if self._task and self._task.isRunning():
            self.status.setText("操作进行中，请稍候")
            return
        self._task = AdbTask(fn, args, self)
        self._task.done.connect(on_done)
        self._task.fail.connect(lambda m: self.status.setText(f"操作失败: {m}"))
        self._task.start()

    def _uninstall(self):
        pkg = self._current()
        if not pkg:
            return
        if QMessageBox.question(self, "卸载", f"确定卸载 {pkg} 吗？") != QMessageBox.StandardButton.Yes:
            return
        self.status.setText(f"正在卸载 {pkg}...")
        self._run_task(self.adb.uninstall, (pkg,), lambda _: self._load())

    def _start(self):
        pkg = self._current()
        if pkg:
            self._run_task(self.adb.start_app, (pkg,), lambda _: None)

    def _stop(self):
        pkg = self._current()
        if pkg:
            self._run_task(self.adb.force_stop, (pkg,), lambda _: None)

    def _clear(self):
        pkg = self._current()
        if not pkg:
            return
        if QMessageBox.question(self, "清数据", f"确定清除 {pkg} 的所有数据吗？") != QMessageBox.StandardButton.Yes:
            return
        self.status.setText(f"正在清除 {pkg} 数据...")
        self._run_task(self.adb.clear_app, (pkg,), lambda _: None)

    def closeEvent(self, ev):
        if self._enrich and self._enrich.isRunning():
            self._enrich._stop = True
            self._enrich.wait(3000)
        super().closeEvent(ev)


# ---------- logcat ----------


class LogcatReader(QThread):
    line = pyqtSignal(str)
    done = pyqtSignal(int)

    def __init__(self, adb: AdbClient, parent=None):
        super().__init__(parent)
        self.adb = adb
        self._stop = False

    def run(self):
        cmd = [self.adb.adb_path]
        if self.adb.device:
            cmd += ["-s", self.adb.device]
        cmd += ["logcat", "-v", "threadtime", "-d", "*:V"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except OSError as e:
            self.line.emit(f"[错误] {e}")
            self.done.emit(0)
            return
        count = 0
        while not self._stop:
            out = proc.stdout.readline()
            if not out:
                if proc.poll() is not None:
                    break
                continue
            self.line.emit(out.rstrip("\n"))
            count += 1
        try:
            proc.kill()
        except OSError:
            pass
        self.done.emit(count)


class LogcatDialog(QDialog):
    _LV_ORDER: Final = {"V": 0, "D": 1, "I": 2, "W": 3, "E": 4}
    _TIME_RE = re.compile(
        r"^(\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+"
        r"(\d+)\s+(\d+)\s+([VDIWEF])\s+(\S+):\s?(.*)$"
    )
    _LV_COLOR: Final = {
        "E": "#e53935",
        "W": "#fb8c00",
        "I": "#1e88e5",
        "D": "#616161",
        "V": "#9e9e9e",
    }

    def __init__(self, adb: AdbClient, parent=None):
        super().__init__(parent)
        self.adb = adb
        self.setWindowTitle("Logcat 日志")
        self.resize(900, 560)
        self._rows: list[dict] = []
        self._filtered: list[int] = []
        self._grabbing = False

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.level = QComboBox(self)
        self.level.addItems(["V", "D", "I", "W", "E"])
        self.level.setCurrentText("D")
        self.level.currentTextChanged.connect(lambda _: self._apply_filter())
        self.tag = QComboBox(self)
        self.tag.setMinimumWidth(110)
        self.tag.currentTextChanged.connect(lambda _: self._apply_filter())
        self.span = QComboBox(self)
        self.span.addItems(["全部时间", "最近5分钟", "最近15分钟", "最近1小时"])
        self.span.currentTextChanged.connect(lambda _: self._apply_filter())
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("过滤关键字…")
        self.search.textChanged.connect(lambda _: self._apply_filter())
        self.grab_btn = QPushButton("抓取", self)
        self.grab_btn.clicked.connect(self._grab)
        self.export_btn = QPushButton("导出", self)
        self.export_btn.clicked.connect(self._export)
        self.clear_btn = QPushButton("清空", self)
        self.clear_btn.clicked.connect(self._clear)
        for w in (
            (QLabel("级别", self), self.level),
            (QLabel("标签", self), self.tag),
            (QLabel("时间", self), self.span),
        ):
            top.addWidget(w[0])
            top.addWidget(w[1])
        top.addWidget(self.search, 1)
        top.addWidget(self.grab_btn)
        top.addWidget(self.export_btn)
        top.addWidget(self.clear_btn)
        lay.addLayout(top)

        self.model = QStandardItemModel(self)
        self.view = QListView(self)
        self.view.setModel(self.model)
        self.view.setUniformItemSizes(True)
        self.view.setFont(QFont("Menlo", 11))
        lay.addWidget(self.view, 1)

        self.status = QLabel("", self)
        lay.addWidget(self.status)
        self._grab()

    def _grab(self):
        if self._grabbing:
            return
        self._grabbing = True
        self.grab_btn.setEnabled(False)
        self.status.setText("正在抓取日志...")
        self._reader = LogcatReader(self.adb, self)
        self._reader.line.connect(self._collect)
        self._reader.done.connect(self._on_done)
        self._reader.start()

    def _collect(self, line: str):
        m = self._TIME_RE.match(line)
        if not m:
            return
        t, pid, tid, lv, tag, msg = m.groups()
        self._rows.append({"time": t, "pid": pid, "tid": tid, "level": lv, "tag": tag, "msg": msg})

    def _on_done(self, count: int):
        self._grabbing = False
        self.grab_btn.setEnabled(True)
        tags = sorted({r["tag"] for r in self._rows})
        cur = self.tag.currentText()
        self.tag.blockSignals(True)
        self.tag.clear()
        self.tag.addItem("全部标签")
        self.tag.addItems(tags)
        idx = self.tag.findText(cur)
        self.tag.setCurrentIndex(max(idx, 0))
        self.tag.blockSignals(False)
        self.status.setText(f"已抓取 {count} 行（共 {len(self._rows)} 条）")
        self._apply_filter()

    def _apply_filter(self):
        lv_min = self._LV_ORDER.get(self.level.currentText(), 0)
        tag = self.tag.currentText()
        kw = self.search.text().strip().lower()
        span_min = {
            "最近5分钟": 5,
            "最近15分钟": 15,
            "最近1小时": 60,
        }.get(self.span.currentText())
        now = time.localtime()
        now_min = now.tm_hour * 60 + now.tm_min
        self._filtered = []
        for i, r in enumerate(self._rows):
            if self._LV_ORDER.get(r["level"], 5) < lv_min:
                continue
            if tag != "全部标签" and r["tag"] != tag:
                continue
            if kw and kw not in (r["tag"] + " " + r["msg"]).lower():
                continue
            if span_min is not None:
                hh = int(r["time"][6:8])
                mm = int(r["time"][9:11])
                diff = now_min - (hh * 60 + mm)
                if diff < 0:
                    diff += 1440
                if diff > span_min:
                    continue
            self._filtered.append(i)
        self._rebuild()

    def _rebuild(self):
        self.model.clear()
        for i in self._filtered:
            r = self._rows[i]
            text = f"{r['time']} {r['pid']} {r['level']}/{r['tag']}: {r['msg']}"
            item = QStandardItem(text)
            item.setEditable(False)
            item.setToolTip(text)
            color = self._LV_COLOR.get(r["level"])
            if color:
                item.setForeground(QColor(color))
            self.model.appendRow(item)
        self.status.setText(f"共 {len(self._rows)} 行，显示 {len(self._filtered)} 行")
        sb = self.view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _export(self):
        if not self.model.rowCount():
            QMessageBox.information(self, "导出", "当前没有可导出的日志")
            return
        default = os.path.join(_SAVE_DIR, f"logcat_{datetime.now():%Y%m%d_%H%M%S}.txt")
        path, _ = QFileDialog.getSaveFileName(self, "导出日志", default, "文本文件 (*.txt)")
        if not path:
            return
        lines = [self.model.item(i).text() for i in range(self.model.rowCount())]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        self.status.setText(f"已导出 {len(lines)} 行到 {path}")

    def _clear(self):
        self.model.clear()
        self._rows = []
        self._filtered = []
        self.status.setText("已清空")

    def closeEvent(self, ev):
        r = getattr(self, "_reader", None)
        if r:
            r._stop = True
            r.wait(2000)
        super().closeEvent(ev)


# ---------- screen recording ----------


class RecordDialog(QDialog):
    def __init__(self, adb: AdbClient, parent=None):
        super().__init__(parent)
        self.adb = adb
        self.setWindowTitle("屏幕录制")
        self.remote = adb.start_recording()
        self.started = time.time()

        lay = QVBoxLayout(self)
        self.status = QLabel("正在录制（手机端）... 点击停止并保存到电脑", self)
        lay.addWidget(self.status)
        btns = QHBoxLayout()
        stop_btn = QPushButton("停止并保存", self)
        stop_btn.clicked.connect(self._stop)
        cancel_btn = QPushButton("取消(不保存)", self)
        cancel_btn.clicked.connect(self._cancel)
        btns.addWidget(stop_btn)
        btns.addWidget(cancel_btn)
        lay.addLayout(btns)

    def _stop(self):
        try:
            self.adb.stop_recording()
            dest = os.path.join(_SAVE_DIR, f"录屏_{datetime.now():%Y%m%d_%H%M%S}.mp4")
            self.adb.pull(self.remote, dest)
            self.status.setText("正在转换格式...")
            if self._remux(dest):
                QMessageBox.information(self, "录屏", f"已保存到:\n{dest}")
                self.accept()
            else:
                QMessageBox.warning(
                    self,
                    "格式提示",
                    f"已保存到:\n{dest}\n\n该文件为流式 MP4，若播放异常，可安装 ffmpeg 或使用 VLC 播放。",
                )
                self.accept()
        except AdbError as e:
            self.status.setText(f"保存失败: {e}")

    def _remux(self, dest: str) -> bool:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return False
        tmp = dest + ".tmp.mp4"
        try:
            proc = subprocess.run(
                [ffmpeg, "-y", "-i", dest, "-c", "copy", "-movflags", "+faststart", tmp],
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            if proc.returncode == 0 and os.path.getsize(tmp) > 0:
                os.replace(tmp, dest)
                return True
        except Exception:
            pass
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False

    def _cancel(self):
        try:
            self.adb.stop_recording()
            self.adb.remove(self.remote)
        except AdbError:
            pass
        self.reject()

    def closeEvent(self, ev):
        try:
            self.adb.stop_recording()
        except Exception:
            pass
        super().closeEvent(ev)


# ---------- system toggles ----------


def _switch_images() -> tuple[str, str]:
    base = os.path.dirname(_CACHE_DIR)
    os.makedirs(base, exist_ok=True)
    on_path = os.path.join(base, "switch_on.png")
    off_path = os.path.join(base, "switch_off.png")
    for on, path in ((True, on_path), (False, off_path)):
        if os.path.isfile(path):
            continue
        pm = QPixmap(46, 26)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = QColor(0x34, 0xC7, 0x59) if on else QColor(0xE8, 0xE8, 0xEB)
        p.setBrush(bg)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(0, 0, 46, 26, 13, 13)
        r = (26 - 4) / 2
        cx = 46 - 13 - 1 if on else 13 + 1
        p.setBrush(QColor(0xFF, 0xFF, 0xFF))
        p.drawEllipse(QPointF(cx, 13), r, r)
        p.end()
        pm.save(path)
    return on_path, off_path


class ToggleDialog(QDialog):
    def __init__(self, adb: AdbClient, parent=None):
        super().__init__(parent)
        self.adb = adb
        self.setWindowTitle("系统开关")
        self.setMinimumWidth(360)
        self._busy: set[QCheckBox] = set()
        self._scale_timer = QTimer(self)
        self._scale_timer.setSingleShot(True)
        self._scale_timer.timeout.connect(self._apply_scale)

        lay = QVBoxLayout(self)
        self.wifi, wl = self._make_row("Wi-Fi", self._set_wifi)
        self.bt, bl = self._make_row("蓝牙", self._set_bt)
        self.stay, sl = self._make_row("充电时屏幕常亮", self._set_stay)
        for row in (wl, bl, sl):
            lay.addLayout(row)
        form = QFormLayout()
        self.scale = QDoubleSpinBox(self)
        self.scale.setRange(0, 10)
        self.scale.setSingleStep(0.5)
        self.scale.setValue(1.0)
        self.scale.setDecimals(1)
        self.scale.valueChanged.connect(lambda _: self._scale_timer.start(400))
        form.addRow("动画缩放(0=关闭)", self.scale)
        lay.addLayout(form)
        self.status = QLabel("", self)
        lay.addWidget(self.status)
        self._load()

    def _make_row(self, text, handler):
        on_img, off_img = _switch_images()
        row = QHBoxLayout()
        cb = QCheckBox(text, self)
        cb.setStyleSheet(
            f"""QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 46px; height: 26px; }}
QCheckBox::indicator:checked {{ image: url("{on_img}"); }}
QCheckBox::indicator:unchecked {{ image: url("{off_img}"); }}"""
        )
        st = QLabel("", self)
        row.addWidget(cb)
        row.addStretch(1)
        row.addWidget(st)
        cb.toggled.connect(handler)
        cb.toggled.connect(lambda on, l=st: l.setText("开" if on else "关"))
        cb.st = st
        return cb, row

    def _load(self):
        for w in (self.wifi, self.bt, self.stay, self.scale):
            w.setEnabled(False)
        self.status.setText("正在读取状态...")
        self._t = AdbTask(self._read_states, (), self)
        self._t.done.connect(self._on_states)
        self._t.fail.connect(lambda m: self.status.setText(f"读取失败: {m}"))
        self._t.start()

    def _read_states(self):
        return (
            self.adb.get_wifi_state(),
            self.adb.get_bluetooth_state(),
            self.adb.get_stay_awake(),
            self.adb.get_anim_scale(),
        )

    def _on_states(self, states):
        wifi, bt, stay, scale = states
        for w, val in (
            (self.wifi, wifi),
            (self.bt, bt),
            (self.stay, stay),
        ):
            w.blockSignals(True)
            w.setChecked(val)
            w.blockSignals(False)
            w.st.setText("开" if val else "关")
        self.scale.blockSignals(True)
        self.scale.setValue(scale)
        self.scale.blockSignals(False)
        for w in (self.wifi, self.bt, self.stay, self.scale):
            w.setEnabled(True)
        self.status.setText("开关状态与设备同步，拨动即生效")

    def _apply_toggle(self, cb: QCheckBox, fn, name: str):
        if cb in self._busy:
            cb.setChecked(not cb.isChecked())
            return
        self._busy.add(cb)
        cb.setEnabled(False)
        self.status.setText(f"正在{'开启' if cb.isChecked() else '关闭'} {name}...")
        self._t = AdbTask(fn, (), self)
        self._t.done.connect(lambda _, c=cb: self._on_toggle_done(c))
        self._t.fail.connect(lambda m, c=cb: self._on_toggle_fail(c, m))
        self._t.start()

    def _on_toggle_done(self, cb: QCheckBox):
        self._busy.discard(cb)
        cb.setEnabled(True)
        self.status.setText(f"已{'开启' if cb.isChecked() else '关闭'}")

    def _on_toggle_fail(self, cb: QCheckBox, msg: str):
        self._busy.discard(cb)
        cb.setEnabled(True)
        cb.blockSignals(True)
        cb.setChecked(not cb.isChecked())
        cb.blockSignals(False)
        cb.st.setText("开" if cb.isChecked() else "关")
        self.status.setText(f"操作失败: {msg}")

    def _set_wifi(self, on: bool):
        self._apply_toggle(self.wifi, lambda: self.adb.set_wifi(on), "Wi-Fi")

    def _set_bt(self, on: bool):
        self._apply_toggle(self.bt, lambda: self.adb.set_bluetooth(on), "蓝牙")

    def _set_stay(self, on: bool):
        self._apply_toggle(self.stay, lambda: self.adb.set_stay_awake(on), "充电常亮")

    def _apply_scale(self):
        self.status.setText("正在设置动画缩放...")
        self._t = AdbTask(lambda: self.adb.set_anim_scale(self.scale.value()), (), self)
        self._t.done.connect(lambda _: self.status.setText("已应用动画缩放"))
        self._t.fail.connect(lambda m: self.status.setText(f"设置失败: {m}"))
        self._t.start()

    def closeEvent(self, ev):
        t = getattr(self, "_t", None)
        if t:
            t.wait(500)
        super().closeEvent(ev)


class FastbootWorker(QThread):
    line = pyqtSignal(str)
    ui = pyqtSignal(object)
    done = pyqtSignal(object)
    fail = pyqtSignal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            result = self.fn(self.line.emit, self.ui.emit)
            self.done.emit([result])
        except FastbootError as e:
            self.fail.emit(str(e))
        except Exception as e:
            self.fail.emit(f"{type(e).__name__}: {e}")


_PARTITIONS = [
    "boot",
    "boot_a",
    "boot_b",
    "init_boot",
    "init_boot_a",
    "init_boot_b",
    "recovery",
    "recovery_a",
    "recovery_b",
    "dtbo",
    "dtbo_a",
    "dtbo_b",
    "vbmeta",
    "vbmeta_system",
    "vbmeta_vendor",
    "vendor_boot",
    "vendor_boot_a",
    "vendor_boot_b",
    "modem",
    "system",
    "system_a",
    "system_b",
    "vendor",
    "vendor_a",
    "vendor_b",
    "super",
]

_QUICK_PARTITIONS = [
    "boot",
    "init_boot",
    "recovery",
    "vendor_boot",
    "dtbo",
    "vbmeta",
    "vbmeta_system",
    "vbmeta_vendor",
    "modem",
    "system",
    "vendor",
    "super",
]


def _quick_partition_names() -> list[str]:
    """常用分区名；A/B 分区补全 _a / _b 双槽，保证下拉列表完整。"""
    names: list[str] = []
    for p in _QUICK_PARTITIONS:
        names.append(p)
        if f"{p}_a" in _PARTITIONS:
            names.append(f"{p}_a")
            names.append(f"{p}_b")
    return names


class FastbootDialog(QDialog):
    def __init__(self, adb: AdbClient, parent=None):
        super().__init__(parent)
        self.adb = adb
        self.fb = FastbootClient()
        self.setWindowTitle("Fastboot 刷机")
        self.resize(820, 650)
        self._worker: FastbootWorker | None = None
        self._cmds: list[dict] = []
        self.device_codename = ""
        self._out_buf: list[str] = []
        self._out_timer = QTimer(self)
        self._out_timer.setInterval(150)
        self._out_timer.timeout.connect(self._flush_out)
        self._out_timer.start()

        lay = QVBoxLayout(self)

        # 顶部固定：设备状态
        dev = QHBoxLayout()
        self.serial_label = QLabel("fastboot 设备: 无", self)
        self.dev_state = QLabel("", self)
        self.reboot_fb_btn = QPushButton("进 fastboot 模式", self)
        self.reboot_fb_btn.clicked.connect(self._reboot_bootloader)
        self.refresh_btn = QPushButton("刷新状态", self)
        self.refresh_btn.clicked.connect(self._refresh)
        dev.addWidget(self.serial_label, 1)
        dev.addWidget(self.dev_state)
        dev.addWidget(self.reboot_fb_btn)
        dev.addWidget(self.refresh_btn)
        lay.addLayout(dev)

        self.info = QLabel("", self)
        self.info.setWordWrap(True)
        lay.addWidget(self.info)

        # 标签页
        self.tabs = QTabWidget(self)
        lay.addWidget(self.tabs)

        # ---- 标签①：分区刷入 ----
        tab_part = QWidget(self)
        vp = QVBoxLayout(tab_part)

        grp1 = QHBoxLayout()
        self.part_name = QLineEdit(tab_part)
        self.part_name.setPlaceholderText("分区名…")
        self.part_add = QPushButton("添加分区", tab_part)
        self.part_add.clicked.connect(self._add_part_row)
        self.part_del = QPushButton("移除所选", tab_part)
        self.part_del.clicked.connect(self._remove_part_rows)
        self.img_add = QPushButton("添加镜像", tab_part)
        self.img_add.clicked.connect(self._add_img_files)
        grp1.addWidget(self.part_name, 1)
        grp1.addWidget(self.part_add)
        grp1.addWidget(self.part_del)
        grp1.addWidget(self.img_add)
        vp.addLayout(grp1)

        # 快捷刷入常用镜像
        grp_q = QHBoxLayout()
        grp_q.addWidget(QLabel("快捷刷入", tab_part))
        self.quick_combo = QComboBox(tab_part)
        self.quick_combo.addItems(_quick_partition_names())
        self.quick_combo.setToolTip("选择要刷入的常用分区")
        self.quick_img_btn = QPushButton("选镜像…", tab_part)
        self.quick_img_btn.clicked.connect(self._quick_pick)
        self.quick_path = ""
        self.quick_add = QPushButton("加入刷写列表", tab_part)
        self.quick_add.clicked.connect(self._quick_add)
        grp_q.addWidget(self.quick_combo)
        grp_q.addWidget(self.quick_img_btn)
        grp_q.addWidget(self.quick_add)
        grp_q.addStretch(1)
        vp.addLayout(grp_q)

        # -- Visual partition flash browser --
        grp_pv = QHBoxLayout()
        self.read_part_btn = QPushButton("读取分区表", tab_part)
        self.read_part_btn.clicked.connect(self._read_partitions)
        grp_pv.addWidget(self.read_part_btn)
        grp_pv.addStretch(1)
        vp.addLayout(grp_pv)

        self.part_browser = QTableWidget(tab_part)
        self.part_browser.setColumnCount(4)
        self.part_browser.setHorizontalHeaderLabels(["", "分区名", "镜像文件", "选择镜像"])
        self.part_browser.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.part_browser.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.part_browser.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.part_browser.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.part_browser.setMaximumHeight(0)
        self.part_browser._part_map = {}
        vp.addWidget(self.part_browser)

        grp_pb = QHBoxLayout()
        self.part_browser_add = QPushButton("添加选中到刷写列表", tab_part)
        self.part_browser_add.clicked.connect(self._add_selected_partitions)
        self.part_browser_add.setVisible(False)
        grp_pb.addWidget(self.part_browser_add)
        grp_pb.addStretch(1)
        self._grp_pb = grp_pb
        vp.addLayout(grp_pb)

        # Partition table
        self.part_table = QTableWidget(tab_part)
        self.part_table.setColumnCount(5)
        self.part_table.setHorizontalHeaderLabels(["刷入", "分区", "镜像文件", "操作", "进度"])
        self.part_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.part_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.part_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.part_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.part_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.part_table.setColumnWidth(4, 120)
        vp.addWidget(self.part_table)

        grp_flash = QHBoxLayout()
        self.wipe_combo = QComboBox(tab_part)
        self.wipe_combo.addItems(["保留数据刷机", "清除数据刷机"])
        self.wipe_combo.setToolTip("清除数据刷机将擦除 userdata 与 metadata，手机数据全部丢失")
        self.auto_reboot = QCheckBox("刷完自动重启", tab_part)
        self.auto_reboot.setToolTip("刷写完成后自动执行 fastboot reboot")
        self.flash_btn = QPushButton("执行刷入", tab_part)
        self.flash_btn.clicked.connect(self._flash_checked)
        grp_flash.addWidget(QLabel("刷机模式", tab_part))
        grp_flash.addWidget(self.wipe_combo)
        grp_flash.addStretch(1)
        grp_flash.addWidget(self.auto_reboot)
        grp_flash.addWidget(self.flash_btn)
        vp.addLayout(grp_flash)
        self.tabs.addTab(tab_part, "分区刷入")

        # ---- 标签②：完整包刷入 ----
        tab_pkg = QWidget(self)
        vk = QVBoxLayout(tab_pkg)

        hint = QLabel("选择完整刷机包（解压目录 / 卡刷 zip / payload.bin），解析后一键刷入。", tab_pkg)
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#888;")
        vk.addWidget(hint)

        # 云提取：粘贴官方全量包链接，下载后可选分区提取或刷入
        cloud = QHBoxLayout()
        self.cloud_url = QLineEdit(tab_pkg)
        self.cloud_url.setPlaceholderText("粘贴 ROM 链接（官方全量包 zip / payload.bin URL）…")
        self.cloud_dl_btn = QPushButton("下载并解包", tab_pkg)
        self.cloud_dl_btn.clicked.connect(self._cloud_download)
        cloud.addWidget(self.cloud_url, 1)
        cloud.addWidget(self.cloud_dl_btn)
        vk.addLayout(cloud)

        cloud2 = QHBoxLayout()
        self.cloud_bar = QProgressBar(tab_pkg)
        self.cloud_bar.setRange(0, 100)
        self.cloud_bar.setValue(0)
        self.cloud_part_combo = QComboBox(tab_pkg)
        self.cloud_part_combo.setEnabled(False)
        self.cloud_extract_btn = QPushButton("提取所选分区…", tab_pkg)
        self.cloud_extract_btn.setEnabled(False)
        self.cloud_extract_btn.clicked.connect(self._cloud_extract)
        self.cloud_flash_btn = QPushButton("刷入所选分区", tab_pkg)
        self.cloud_flash_btn.setEnabled(False)
        self.cloud_flash_btn.clicked.connect(self._cloud_flash)
        cloud2.addWidget(self.cloud_bar, 1)
        cloud2.addWidget(QLabel("分区", tab_pkg))
        cloud2.addWidget(self.cloud_part_combo)
        cloud2.addWidget(self.cloud_extract_btn)
        cloud2.addWidget(self.cloud_flash_btn)
        vk.addLayout(cloud2)
        self._cloud_payload = ""

        # Package dir row
        grp2b = QHBoxLayout()
        self.pkg_edit = QLineEdit(tab_pkg)
        self.pkg_edit.setPlaceholderText("选择刷机包目录，或卡刷包 zip / payload.bin 文件…")
        self.pkg_browse = QPushButton("选择目录…", tab_pkg)
        self.pkg_browse.clicked.connect(self._browse_pkg)
        self.pkg_browse_file = QPushButton("选 zip/payload…", tab_pkg)
        self.pkg_browse_file.clicked.connect(self._browse_pkg_file)
        self.pkg_parse = QPushButton("解析刷机包", tab_pkg)
        self.pkg_parse.clicked.connect(self._parse_pkg)
        grp2b.addWidget(self.pkg_edit, 1)
        grp2b.addWidget(self.pkg_browse)
        grp2b.addWidget(self.pkg_browse_file)
        grp2b.addWidget(self.pkg_parse)
        vk.addLayout(grp2b)

        self.pkg_preview = QListWidget(tab_pkg)
        self.pkg_preview.setStyleSheet("QListWidget::item { font-family: Menlo; font-size: 11px; }")
        vk.addWidget(self.pkg_preview, 1)

        grp_pkg = QHBoxLayout()
        self.pkg_wipe = QCheckBox("清除数据（擦除 userdata/metadata）", tab_pkg)
        self.pkg_reboot = QCheckBox("刷完自动重启", tab_pkg)
        self.pkg_flash_btn = QPushButton("一键完整刷入", tab_pkg)
        self.pkg_flash_btn.clicked.connect(self._flash_package)
        grp_pkg.addWidget(self.pkg_wipe)
        grp_pkg.addWidget(self.pkg_reboot)
        grp_pkg.addStretch(1)
        grp_pkg.addWidget(self.pkg_flash_btn)
        vk.addLayout(grp_pkg)
        self.tabs.addTab(tab_pkg, "完整包刷入")

        # 底部共享条：输出区 + 设备与重启（分区刷入 / 完整包刷入 共用，位于右下角）
        self.output = QListWidget(self)
        self.output.setStyleSheet("QListWidget::item { font-family: Menlo; font-size: 11px; }")
        lay.addWidget(self.output, 1)

        bottom = QHBoxLayout()
        self.status = QLabel("", self)
        bottom.addWidget(self.status, 1)
        bottom.addStretch(1)
        bottom.addWidget(QLabel("重启", self))
        self.reboot_combo = QComboBox(self)
        self.reboot_combo.addItems(
            ["重启到系统", "重启到 Recovery", "重启到 FastbootD", "重启回 Bootloader", "继续启动(continue)"]
        )
        self.reboot_btn = QPushButton("执行重启", self)
        self.reboot_btn.clicked.connect(self._fb_reboot)
        bottom.addWidget(self.reboot_combo)
        bottom.addWidget(self.reboot_btn)
        lay.addLayout(bottom)
        self._refresh()

    def _refresh(self):
        self.refresh_btn.setEnabled(False)
        self.status.setText("正在检测 fastboot 设备...")
        self._worker = FastbootWorker(lambda cb, ui: self._do_refresh(cb), self)
        self._worker.done.connect(self._on_refresh)
        self._worker.fail.connect(self._on_fail)
        self._worker.start()

    def _do_refresh(self, cb):
        serials = self.fb.devices()
        if not serials:
            self.fb.device = ""
            return "", ""
        self.fb.device = serials[0]
        info = []
        for name in ("product", "unlocked", "slot-count", "current-slot", "max-download-size"):
            v = self.fb.getvar(name)
            info.append(f"{name}: {v}")
        self._last_info = "\n".join(info)
        return serials[0], self._last_info

    def _on_refresh(self, result):
        result = result[0]
        self.refresh_btn.setEnabled(True)
        serial, info = result
        if not serial:
            self.serial_label.setText("fastboot 设备: 无")
            self.dev_state.setText("")
            self.info.setText("请把手机进入 fastboot 模式（关机后同时按音量下+电源，或用'进 fastboot 模式'按钮）")
            self.status.setText("未检测到 fastboot 设备")
            return
        self.serial_label.setText(f"fastboot 设备: {serial}")
        m = re.search(r"unlocked:\s*(\S+)", info)
        unlocked = bool(m and m.group(1).lower() in ("yes", "unlocked", "1"))
        self.dev_state.setText("BL 已解锁" if unlocked else ("BL 已锁" if m else "状态未知"))
        pm = re.search(r"product:\s*(\S+)", info)
        self.device_codename = pm.group(1) if pm else ""
        self.info.setText(info)
        self.status.setText(f"已连接 fastboot 设备 {serial}")

    def _reboot_bootloader(self):
        if not self.adb.device:
            self.status.setText("请先通过 adb 连接手机")
            return
        self.status.setText("正在重启到 bootloader...")
        self._worker = FastbootWorker(
            lambda cb, ui: (cb("adb reboot bootloader"), self.adb._run(["reboot", "bootloader"]))[1], self
        )
        self._worker.done.connect(lambda _: self.status.setText("已发送重启指令，等待设备进入 fastboot..."))
        self._worker.fail.connect(self._on_fail)
        self._worker.start()

    def _browse_pkg(self):
        path = QFileDialog.getExistingDirectory(self, "选择刷机包目录", os.path.expanduser("~/Downloads"))
        if path:
            self.pkg_edit.setText(path)

    def _browse_pkg_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择卡刷包", os.path.expanduser("~/Downloads"), "卡刷包 (*.zip *.bin);;所有文件 (*)"
        )
        if path:
            self.pkg_edit.setText(path)

    def _start_parse(self):
        pkg = self.pkg_edit.text().strip()
        if not os.path.exists(pkg):
            self.status.setText("请选择有效的刷机包目录、zip 或 payload.bin 文件")
            return
        self.pkg_parse.setEnabled(False)
        self.output.clear()
        self.status.setText("正在解析刷机包（解包时可能较慢，请稍候）...")

        def work(cb, ui):
            if os.path.isdir(pkg):
                return self.fb.parse_package(pkg, line_cb=cb)
            if pkg.lower().endswith(".zip"):
                return self.fb.parse_zip(pkg, line_cb=cb)
            return self.fb.parse_payload(pkg, line_cb=cb)

        self._worker = FastbootWorker(work, self)
        self._worker.line.connect(self._queue_out)
        self._worker.done.connect(self._on_pkg_parsed)
        self._worker.fail.connect(self._on_pkg_parse_fail)
        self._worker.start()

    def _parse_pkg(self):
        self._start_parse()

    def _on_pkg_parsed(self, result):
        self.pkg_parse.setEnabled(True)
        result = result[0]
        self._pkg = self.pkg_edit.text().strip()
        self._pkg_meta = result
        self._cmds = list(result["commands"])
        boot_choices = result.get("boot_choices") or []
        if boot_choices:
            boot_cmd = self._offer_boot_choice(boot_choices)
            if boot_cmd:
                self._cmds.append(boot_cmd)
        self._rebuild_pkg_view(result)

    def _offer_boot_choice(self, boot_choices) -> dict | None:
        """第三方自定义 ROM：boot 内核需从 Rootkernel/kernel 二选一刷入 boot_ab。"""
        items = [b["label"] for b in boot_choices]
        skip_label = "暂不选择，跳过刷入 boot（不推荐）"
        items.append(skip_label)
        current = 1 if len(boot_choices) >= 2 else 0  # 默认无 Root 官方内核
        text, ok = QInputDialog.getItem(
            self,
            "选择要刷入的内核",
            "此刷机包需要把内核刷入 boot_ab，请选择版本：",
            items,
            current,
            False,
        )
        if not ok or text == skip_label:
            self.status.setText("未选择内核，boot 分区将不会被刷入")
            return None
        b = boot_choices[items.index(text)]
        return {
            "tool": "fastboot",
            "args": ["flash", "boot_ab", b["path"]],
            "raw": f"flash boot_ab ← {os.path.basename(b['path'])}",
        }

    def _rebuild_pkg_view(self, result: dict):
        self.pkg_preview.clear()
        for i, c in enumerate(self._cmds, 1):
            self.pkg_preview.addItem(f"[{i}/{len(self._cmds)}] {c['raw']}")
        self.part_table.setRowCount(0)
        seen = set()
        for c in self._cmds:
            args = c["args"]
            if "flash" not in args:
                continue
            fi = args.index("flash")
            img = ""
            for a in args[fi + 1 :]:
                if a.endswith(
                    (".img", ".img.zst", ".lz4", ".zip", ".dat", ".elf", ".melf", ".mbn", ".bin", ".fv", ".txt")
                ):
                    img = a
            part = ""
            for a in args[fi + 1 :]:
                if a.startswith("-"):
                    continue
                if a == img:
                    break
                part = a
                break
            if not part:
                continue
            if part in seen:
                continue
            seen.add(part)
            self._insert_part_row(part, img)
        self._resize_table_to_rows()
        rd = result["right_device"]
        scheme = result.get("source", "")
        if scheme == "custom_ab":
            ab = "自定义 _ab 槽"
        elif result["ab"]:
            ab = "A/B 双槽"
        else:
            ab = "A-only"
        self.status.setText(
            f"已解析刷机包（{ab}）{len(self._cmds)} 条命令 / {len(seen)} 个分区" + (f"，机型: {rd}" if rd else "")
        )
        if rd and self.device_codename and rd != self.device_codename:
            QMessageBox.warning(
                self,
                "机型不匹配",
                f"此刷机包是给 {rd} 机型的，当前设备是 {self.device_codename}。\n\n"
                "强行刷入极可能导致变砖！\n除非你确定这是通用包，否则请中止。",
            )

    def _on_pkg_parse_fail(self, msg):
        self.pkg_parse.setEnabled(True)
        self.status.setText(f"X 解析失败: {msg}")

    # ---------- 云提取：下载 ROM 链接并选分区提取/刷入 ----------
    def _cloud_download(self):
        url = self.cloud_url.text().strip()
        if not url.startswith(("http://", "https://")):
            self.status.setText("请输入有效的 ROM 链接")
            return
        self.cloud_dl_btn.setEnabled(False)
        self.cloud_bar.setValue(0)
        self.cloud_part_combo.clear()
        self.cloud_part_combo.setEnabled(False)
        self.cloud_extract_btn.setEnabled(False)
        self.cloud_flash_btn.setEnabled(False)
        self._cloud_payload = ""
        self.output.clear()
        self.status.setText("正在下载 ROM 包...")
        self._queue_out("开始下载: " + url)

        def work(cb, ui):
            name = os.path.basename(urlparse(url).path) or "rom"
            if "." not in name:
                name += ".bin"
            ddir = os.path.join(_SAVE_DIR, "rom_downloads")
            dest = os.path.join(ddir, name)
            if os.path.exists(dest):
                base, ext = os.path.splitext(name)
                i = 1
                while os.path.exists(dest):
                    dest = os.path.join(ddir, f"{base}_{i}{ext}")
                    i += 1

            def prog(done, total):
                if total:
                    ui(("progress", int(done * 100 / total)))
                    ui(("status", f"下载中 {done / 1048576:.1f}/{total / 1048576:.1f} MB"))
                else:
                    ui(("status", f"下载中 {done / 1048576:.1f} MB"))

            payload_dumper.download(url, dest, progress=prog)
            ui(("progress", 100))
            ui(("status", "下载完成，正在识别与解析..."))
            cb(f"下载完成: {dest}")
            p = dest
            if not payload_dumper.is_payload(p):
                with open(p, "rb") as f:
                    if f.read(4) != b"PK\x03\x04":
                        raise payload_dumper.PayloadError("下载的文件既不是 payload.bin 也不是 zip 压缩包")
                p = payload_dumper.extract_payload_from_zip(dest, os.path.join(ddir, ".cloud_payload"))
                cb("已从 zip 中提取 payload.bin")
            parts = payload_dumper.list_partitions(p)
            return p, parts

        self._worker = FastbootWorker(work, self)
        self._worker.line.connect(self._queue_out)
        self._worker.ui.connect(self._on_worker_ui)
        self._worker.done.connect(self._on_cloud_parsed)
        self._worker.fail.connect(self._on_cloud_fail)
        self._worker.start()

    def _on_cloud_parsed(self, result):
        self.cloud_dl_btn.setEnabled(True)
        p, parts = result[0]
        self._cloud_payload = p
        self.cloud_part_combo.clear()
        self.cloud_part_combo.addItems(parts)
        self.cloud_part_combo.setEnabled(True)
        self.cloud_extract_btn.setEnabled(True)
        self.cloud_flash_btn.setEnabled(True)
        self.status.setText(f"已解析，共 {len(parts)} 个分区，可提取或刷入所选分区。")

    def _on_cloud_fail(self, msg):
        self.cloud_dl_btn.setEnabled(True)
        self.cloud_part_combo.setEnabled(False)
        self.cloud_extract_btn.setEnabled(False)
        self.cloud_flash_btn.setEnabled(False)
        self.status.setText(f"X 云提取失败: {msg}")
        self.output.addItem(f"X {msg}")

    def _cloud_extract(self):
        if not self._cloud_payload:
            self.status.setText("请先下载并解析 ROM 包")
            return
        part = self.cloud_part_combo.currentText()
        out = QFileDialog.getExistingDirectory(self, "选择提取保存目录", _SAVE_DIR)
        if not out:
            return
        self.cloud_extract_btn.setEnabled(False)
        self.status.setText(f"正在提取 {part} ...")

        def work(cb, ui):
            payload_dumper.extract_partitions(self._cloud_payload, out, [part], line_cb=cb)
            ui(("out", f"OK 已提取 {part}.img 到 {out}"))

        self._worker = FastbootWorker(work, self)
        self._worker.line.connect(self._queue_out)
        self._worker.ui.connect(self._on_worker_ui)
        self._worker.done.connect(
            lambda _: (self.cloud_extract_btn.setEnabled(True), self.status.setText(f"已提取 {part}.img"))[1]
        )
        self._worker.fail.connect(self._on_fail)
        self._worker.start()

    def _cloud_flash(self):
        if not self._cloud_payload:
            self.status.setText("请先下载并解析 ROM 包")
            return
        if not self.fb.device:
            self.status.setText("请先连接 fastboot 设备")
            return
        part = self.cloud_part_combo.currentText()
        tmp = tempfile.mkdtemp(prefix="adbtool_cloud_")
        self._cloud_tmp = tmp
        self.output.clear()
        self.status.setText(f"正在提取并刷入 {part} ...")
        self._set_flashing(True)

        def work(cb, ui):
            payload_dumper.extract_partitions(self._cloud_payload, tmp, [part], line_cb=cb)
            img = os.path.join(tmp, f"{part}.img")
            if not os.path.isfile(img):
                raise payload_dumper.PayloadError(f"提取后未找到 {part}.img")
            cb(f"flash {part} ← {os.path.basename(img)}")
            self.fb.flash(part, img, line_cb=cb)
            ui(("out", f"OK {part} 刷入完成"))

        self._worker = FastbootWorker(work, self)
        self._worker.line.connect(self._queue_out)
        self._worker.ui.connect(self._on_worker_ui)
        self._worker.done.connect(self._on_cloud_flash_done)
        self._worker.fail.connect(self._on_fail)
        self._worker.start()

    def _on_cloud_flash_done(self, _):
        self._set_flashing(False)
        self.status.setText("OK 刷入完成")
        tmp = getattr(self, "_cloud_tmp", None)
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
            self._cloud_tmp = ""

    def _resize_table_to_rows(self):
        rows = self.part_table.rowCount()
        if rows == 0:
            self.part_table.setMaximumHeight(60)
            return
        row_h = self.part_table.verticalHeader().defaultSectionSize()
        hdr = self.part_table.horizontalHeader().height() or 28
        target = min(400, max(60, hdr + rows * row_h + 4))
        self.part_table.setMaximumHeight(target)

    def _add_part_row(self):
        part = self.part_name.text().strip()
        if not part:
            self.status.setText("请输入分区名")
            return
        self._insert_part_row(part)
        self._resize_table_to_rows()
        self.part_name.clear()
        self.status.setText(f"已添加分区 {part}")

    def _insert_part_row(self, part: str, img: str = "", checked: bool = True):
        row = self.part_table.rowCount()
        self.part_table.insertRow(row)
        ck = QTableWidgetItem()
        ck.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        ck.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.part_table.setItem(row, 0, ck)
        self.part_table.setItem(row, 1, QTableWidgetItem(part))
        img_item = QTableWidgetItem(img)
        img_item.setToolTip(img)
        self.part_table.setItem(row, 2, img_item)
        btn = QPushButton("浏览…", self)
        btn.setFixedWidth(60)
        btn.clicked.connect(lambda _, r=row: self._row_browse(r))
        self.part_table.setCellWidget(row, 3, btn)
        bar = QProgressBar(self)
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setTextVisible(True)
        self.part_table.setCellWidget(row, 4, bar)

    def _remove_part_rows(self):
        rows = sorted({i.row() for i in self.part_table.selectedItems()}, reverse=True)
        for r in rows:
            self.part_table.removeRow(r)
        self._resize_table_to_rows()
        self.status.setText(f"已移除 {len(rows)} 行")

    def _row_browse(self, row: int):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择镜像", os.path.expanduser("~/Desktop"), "镜像文件 (*.img *.lz4)"
        )
        if path:
            self._set_row_img(row, path)

    def _set_row_img(self, row: int, path: str):
        item = self.part_table.item(row, 2)
        if item:
            item.setText(path)
            item.setToolTip(path)
        # 替换浏览按钮为可再编辑（保留按钮即可，tooltip 已有路径）

    def _quick_pick(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择镜像", os.path.expanduser("~/Desktop"), "镜像文件 (*.img *.img.zst *.lz4);;所有文件 (*)"
        )
        if path:
            self.quick_path = path
            self.quick_img_btn.setText(f"已选: {os.path.basename(path)}")
            self.status.setText(f"镜像已选: {path}")

    def _quick_add(self):
        if not self.quick_path:
            self.status.setText("请先点「选镜像…」选择镜像文件")
            return
        part = self.quick_combo.currentText()
        self._insert_part_row(part, self.quick_path)
        self._resize_table_to_rows()
        self.status.setText(f"已加入 {part} ← {os.path.basename(self.quick_path)}")

    # -- Visual partition flash methods --
    def _read_partitions(self):
        if not self.fb.device:
            self.status.setText("请先连接 fastboot 设备")
            return
        self.read_part_btn.setEnabled(False)
        self.status.setText("正在读取分区表...")
        self._worker = FastbootWorker(
            lambda cb, ui: self.fb.get_partition_list(line_cb=cb),
            self,
        )
        self._worker.done.connect(self._on_partitions_read)
        self._worker.fail.connect(self._on_fail)
        self._worker.start()

    def _on_partitions_read(self, result):
        partitions = result[0]
        self.read_part_btn.setEnabled(True)
        if not partitions:
            self.status.setText("未读取到分区信息")
            return
        self.part_browser.setRowCount(0)
        self.part_browser._part_map = {}
        for i, name in enumerate(partitions):
            row = self.part_browser.rowCount()
            self.part_browser.insertRow(row)
            ck = QTableWidgetItem()
            ck.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            ck.setCheckState(Qt.CheckState.Unchecked)
            self.part_browser.setItem(row, 0, ck)
            self.part_browser.setItem(row, 1, QTableWidgetItem(name))
            self.part_browser.setItem(row, 2, QTableWidgetItem(""))
            btn = QPushButton("选择…", self)
            btn.setFixedWidth(70)
            btn.clicked.connect(lambda _, r=row: self._browse_part_img(r))
            self.part_browser.setCellWidget(row, 3, btn)
            self.part_browser._part_map[row] = name
        self.part_browser.setMaximumHeight(min(300, 60 + len(partitions) * 28))
        self.part_browser_add.setVisible(True)
        self.status.setText(f"已读取 {len(partitions)} 个分区，勾选并选择镜像后点「添加选中到刷写列表」")

    def _browse_part_img(self, row):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择镜像", os.path.expanduser("~/Desktop"), "镜像文件 (*.img *.img.zst *.lz4);;所有文件 (*)"
        )
        if path:
            self.part_browser.item(row, 2).setText(path)

    def _add_selected_partitions(self):
        count = 0
        for row in range(self.part_browser.rowCount()):
            ck = self.part_browser.item(row, 0)
            if ck and ck.checkState() == Qt.CheckState.Checked:
                part = self.part_browser._part_map.get(row, "")
                img_item = self.part_browser.item(row, 2)
                img = img_item.text().strip() if img_item else ""
                if not part:
                    continue
                self._insert_part_row(part, img)
                count += 1
        self._resize_table_to_rows()
        if count:
            self.status.setText(f"已添加 {count} 个分区到刷写列表")
        else:
            self.status.setText("请先勾选要刷入的分区")

    def _add_img_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择镜像文件", os.path.expanduser("~/Desktop"), "镜像文件 (*.img *.img.zst *.lz4);;所有文件 (*)"
        )
        if not files:
            return
        count = 0
        for f in files:
            name = os.path.basename(f)
            part = name.lower()
            for suffix in (".img", ".img.zst", ".lz4"):
                if part.endswith(suffix):
                    part = part[: -len(suffix)]
                    break
            for prefix in ("image-", "flash_"):
                part = part.removeprefix(prefix)
            self._insert_part_row(part, f)
            count += 1
        self._resize_table_to_rows()
        self.status.setText("已添加 " + str(count) + " 个镜像")

    def _load_pkg_rows(self):
        self._start_parse()

    _GREEN = "QProgressBar::chunk { background-color: #4caf50; }"
    _RED = "QProgressBar::chunk { background-color: #f44336; }"
    _BLUE = "QProgressBar::chunk { background-color: #2196f3; }"

    def _flash_checked(self):
        rows = []
        for r in range(self.part_table.rowCount()):
            ck = self.part_table.item(r, 0)
            if ck and ck.checkState() == Qt.CheckState.Checked:
                part = self.part_table.item(r, 1).text().strip()
                img = self.part_table.item(r, 2).text().strip()
                rows.append((r, part, img))
        if not rows:
            self.status.setText("请先勾选要刷入的分区")
            return
        bad = [p for r, p, i in rows if not os.path.isfile(i)]
        if bad:
            self.status.setText(f"镜像文件不存在: {bad[0]}")
            return
        wipe = self.wipe_combo.currentText() == "清除数据刷机"
        msg = f"将按顺序刷入 {len(rows)} 个分区：\n\n" + "\n".join(f"  {p} ← {os.path.basename(i)}" for r, p, i in rows)
        if wipe:
            msg += "\n\n警告: 模式：清除数据刷机\n刷完后将擦除 userdata、metadata，"
            msg += "手机上所有数据都会丢失且不可恢复！"
        msg += "\n\n刷写中不要断开 USB！确定继续？"
        ret = QMessageBox.warning(
            self,
            "确认刷入" + ("（清除数据）" if wipe else "（保留数据）"),
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        self.output.clear()
        self.status.setText(f"正在刷入 {len(rows)} 个分区...")
        self._set_flashing(True)

        def work(cb, ui):
            for r, p, i in rows:
                cb(f"flash {p} ← {os.path.basename(i)}")
                ui(("bar", r, 30, self._BLUE))
                sent = [False]

                def on_line(text: str, row=r, sent=sent):
                    if not sent[0] and "Sending" in text:
                        sent[0] = True
                        ui(("bar", row, 30, self._BLUE))
                    elif sent[0] and "Writing" in text:
                        sent[0] = False
                        ui(("bar", row, 70, self._BLUE))
                    cb(text)

                try:
                    self.fb.flash(p, i, line_cb=on_line)
                except Exception:
                    ui(("bar", r, 100, self._RED))
                    raise
                ui(("bar", r, 100, self._GREEN))
                ui(("out", f"OK {p} 完成"))
            if wipe:
                for w in ("userdata", "metadata"):
                    cb(f"erase {w}")
                    self.fb.erase(w, line_cb=cb)
                    ui(("out", f"OK erase {w} 完成"))
            if self.auto_reboot.isChecked():
                cb("reboot → system")
                self.fb.reboot(line_cb=cb)
                ui(("out", "OK 已发送重启指令（system）"))

        self._worker = FastbootWorker(work, self)
        self._worker.line.connect(self._queue_out)
        self._worker.ui.connect(self._on_worker_ui)
        self._worker.done.connect(self._on_flash_done)
        self._worker.fail.connect(self._on_fail)
        self._worker.start()

    def _on_flash_done(self, _):
        self._set_flashing(False)
        self.status.setText("OK 全部刷入完成")

    def _flash_package(self):
        if not self._cmds:
            self.status.setText("请先在「完整包刷入」页解析刷机包")
            return
        wipe = self.pkg_wipe.isChecked()
        msg = f"将完整刷入 {len(self._cmds)} 条命令。"
        if wipe:
            msg += "\n\n警告: 将擦除 userdata、metadata，手机上所有数据都会丢失且不可恢复！"
        msg += "\n\n刷写中不要断开 USB！确定继续？"
        ret = QMessageBox.warning(
            self,
            "确认完整刷入",
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        self.output.clear()
        self.status.setText(f"正在完整刷入 {len(self._cmds)} 条命令...")
        self._set_flashing(True)

        def work(cb, ui):
            def prog(i, total, raw):
                ui(("status", f"完整刷入中 {i}/{total}"))
                cb(raw)

            self.fb.run_package(self._cmds, line_cb=cb, progress_cb=prog)
            if wipe:
                for w in ("userdata", "metadata"):
                    cb(f"erase {w}")
                    self.fb.erase(w, line_cb=cb)
                    ui(("out", f"OK erase {w} 完成"))
            if self.pkg_reboot.isChecked():
                cb("reboot → system")
                self.fb.reboot(line_cb=cb)
                ui(("out", "OK 已发送重启指令（system）"))

        self._worker = FastbootWorker(work, self)
        self._worker.line.connect(self._queue_out)
        self._worker.ui.connect(self._on_worker_ui)
        self._worker.done.connect(self._on_flash_done)
        self._worker.fail.connect(self._on_fail)
        self._worker.start()

    def _set_flashing(self, flashing: bool):
        for w in (
            self.flash_btn,
            self.refresh_btn,
            self.reboot_fb_btn,
            self.part_add,
            self.part_del,
            self.pkg_parse,
            self.pkg_flash_btn,
            self.reboot_btn,
            self.quick_add,
            self.quick_img_btn,
            self.cloud_dl_btn,
            self.cloud_extract_btn,
            self.cloud_flash_btn,
        ):
            w.setEnabled(not flashing)
        self.part_name.setEnabled(not flashing)
        self.auto_reboot.setEnabled(not flashing)
        self.pkg_wipe.setEnabled(not flashing)
        self.pkg_reboot.setEnabled(not flashing)
        self.wipe_combo.setEnabled(not flashing)
        for r in range(self.part_table.rowCount()):
            ck = self.part_table.item(r, 0)
            if ck:
                ck.setFlags(
                    (Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
                    if not flashing
                    else Qt.ItemFlag.NoItemFlags
                )
            btn = self.part_table.cellWidget(r, 3)
            if btn:
                btn.setEnabled(not flashing)

    def _on_worker_ui(self, op):
        kind = op[0]
        if kind == "bar":
            _, row, value, style = op
            bar = self.part_table.cellWidget(row, 4)
            if bar:
                if style:
                    bar.setStyleSheet(style)
                bar.setValue(value)
        elif kind == "progress":
            self.cloud_bar.setValue(op[1])
        elif kind == "out":
            self._queue_out(op[1])
        elif kind == "status":
            self.status.setText(op[1])

    def _queue_out(self, text: str):
        self._out_buf.append(text)
        if len(self._out_buf) > 2000:
            self._flush_out()

    def _flush_out(self):
        if not self._out_buf:
            return
        lines, self._out_buf = self._out_buf, []
        self.output.setUpdatesEnabled(False)
        try:
            for t in lines:
                self.output.addItem(t)
            while self.output.count() > 2000:
                self.output.takeItem(0)
        finally:
            self.output.setUpdatesEnabled(True)

    def _fb_reboot(self):
        choices = {
            "重启到系统": "system",
            "重启到 Recovery": "recovery",
            "重启到 FastbootD": "fastboot",
            "重启回 Bootloader": "bootloader",
            "继续启动(continue)": "continue",
        }
        action = choices.get(self.reboot_combo.currentText(), "system")
        self.output.clear()
        self.status.setText(f"正在执行 {action} ...")
        self.reboot_btn.setEnabled(False)
        if action == "continue":
            fn = lambda cb, ui: self.fb.continue_boot(line_cb=cb)
        else:
            fn = lambda cb, ui: self.fb.reboot(action, line_cb=cb)
        self._worker = FastbootWorker(fn, self)
        self._worker.done.connect(
            lambda _: (self.reboot_btn.setEnabled(True), self.status.setText(f"OK 已发送重启指令（{action}）"))[1]
        )
        self._worker.fail.connect(self._on_fail)
        self._worker.start()

    def _on_fail(self, msg: str):
        self._set_flashing(False)
        self.status.setText(f"X 失败: {msg}")
        self.output.addItem(f"X {msg}")

    def closeEvent(self, ev):
        w = getattr(self, "_worker", None)
        if w:
            w.wait(1000)
        super().closeEvent(ev)


class OfflinePatchDialog(QDialog):
    """脱机修补：Magisk 修补 / 制作 GKI 镜像 / 内核级 Root。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("脱机修补")
        self.resize(680, 560)
        self._worker: FastbootWorker | None = None

        lay = QVBoxLayout(self)
        tip = QLabel("脱机修补：不连接设备，在电脑端对 boot/init_boot 镜像打补丁，产物用 fastboot 刷入。", self)
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#888;")
        lay.addWidget(tip)

        self.tabs = QTabWidget(self)
        lay.addWidget(self.tabs)

        # ---- Magisk 修补 ----
        tab_mag = QWidget(self)
        vm = QVBoxLayout(tab_mag)

        fm = QFormLayout()
        row1 = QHBoxLayout()
        self.mag_boot = QLineEdit(tab_mag)
        b1 = QPushButton("选择 boot/init_boot…", tab_mag)
        b1.clicked.connect(lambda: self._pick_file(self.mag_boot, "boot/init_boot", "镜像 (*.img)"))
        row1.addWidget(self.mag_boot, 1)
        row1.addWidget(b1)
        fm.addRow("修补镜像", row1)

        row2 = QHBoxLayout()
        self.mag_apk = QLineEdit(tab_mag)
        b2 = QPushButton("选择 Magisk APK…", tab_mag)
        b2.clicked.connect(lambda: self._pick_file(self.mag_apk, "Magisk APK", "APK (*.apk)"))
        row2.addWidget(self.mag_apk, 1)
        row2.addWidget(b2)
        fm.addRow("Magisk 安装包", row2)

        self.mag_abi = QComboBox(tab_mag)
        self.mag_abi.addItems(["arm64-v8a", "armeabi-v7a", "x86_64", "x86"])
        fm.addRow("架构 ABI", self.mag_abi)
        vm.addLayout(fm)

        grp = QHBoxLayout()
        self.opt_keepverity = QCheckBox("保留 AVB2.0/dm-verity", tab_mag)
        self.opt_keepverity.setChecked(True)
        self.opt_keeplen = QCheckBox("保留强制加密", tab_mag)
        self.opt_keeplen.setChecked(True)
        self.opt_recovery = QCheckBox("安装到 Recovery", tab_mag)
        self.opt_patchvbmeta = QCheckBox("修补 vboot 标志", tab_mag)
        self.opt_forcefs = QCheckBox("强制 rootfs", tab_mag)
        for w in (self.opt_keepverity, self.opt_keeplen, self.opt_recovery, self.opt_patchvbmeta, self.opt_forcefs):
            grp.addWidget(w)
        grp.addStretch(1)
        vm.addLayout(grp)

        row4 = QHBoxLayout()
        self.mag_out = QLineEdit(tab_mag)
        self.mag_out.setPlaceholderText(os.path.join(_SAVE_DIR, "offline_patch"))
        b3 = QPushButton("输出目录…", tab_mag)
        b3.clicked.connect(self._pick_outdir)
        row4.addWidget(QLabel("输出目录", tab_mag))
        row4.addWidget(self.mag_out, 1)
        row4.addWidget(b3)
        vm.addLayout(row4)

        self.mag_btn = QPushButton("修补镜像", tab_mag)
        self.mag_btn.clicked.connect(self._magisk_patch)
        vm.addWidget(self.mag_btn)
        self.tabs.addTab(tab_mag, "Magisk 修补")

        # ---- 制作 GKI 镜像 ----
        tab_gki = QWidget(self)
        vg = QVBoxLayout(tab_gki)
        fg = QFormLayout()
        rowb = QHBoxLayout()
        self.gki_boot = QLineEdit(tab_gki)
        bb = QPushButton("选择 boot.img…", tab_gki)
        bb.clicked.connect(lambda: self._pick_file(self.gki_boot, "boot", "镜像 (*.img)"))
        rowb.addWidget(self.gki_boot, 1)
        rowb.addWidget(bb)
        fg.addRow("boot 路径", rowb)
        rowa = QHBoxLayout()
        self.gki_ak3 = QLineEdit(tab_gki)
        ba = QPushButton("选择 AK3…", tab_gki)
        ba.clicked.connect(lambda: self._pick_file(self.gki_ak3, "AnyKernel3", "压缩包 (*.zip)"))
        rowa.addWidget(self.gki_ak3, 1)
        rowa.addWidget(ba)
        fg.addRow("ak3 路径", rowa)
        vg.addLayout(fg)
        rowg = QHBoxLayout()
        self.gki_out = QLineEdit(tab_gki)
        bg = QPushButton("输出目录…", tab_gki)
        bg.clicked.connect(lambda: self._set_outdir(self.gki_out))
        rowg.addWidget(QLabel("输出目录", tab_gki))
        rowg.addWidget(self.gki_out, 1)
        rowg.addWidget(bg)
        vg.addLayout(rowg)
        self.gki_btn = QPushButton("开始制作", tab_gki)
        self.gki_btn.clicked.connect(self._gki_build)
        vg.addWidget(self.gki_btn)
        vg.addStretch(1)
        self.tabs.addTab(tab_gki, "制作 GKI 镜像")

        # ---- KernelSU / SukiSU 内核级 Root ----
        tab_ks = QWidget(self)
        vk = QVBoxLayout(tab_ks)
        fk = QFormLayout()
        rowb2 = QHBoxLayout()
        self.ks_boot = QLineEdit(tab_ks)
        bb2 = QPushButton("选择 boot/init_boot…", tab_ks)
        bb2.clicked.connect(lambda: self._pick_file(self.ks_boot, "boot/init_boot", "镜像 (*.img)"))
        rowb2.addWidget(self.ks_boot, 1)
        rowb2.addWidget(bb2)
        fk.addRow("修补镜像", rowb2)
        self.ks_method = QComboBox(tab_ks)
        self.ks_method.addItems(list(offline_patch.KSU_METHODS))
        fk.addRow("方案", self.ks_method)
        self.ks_kmi = QComboBox(tab_ks)
        self.ks_kmi.addItem("自动检测")
        self.ks_kmi.addItems(list(offline_patch.KMI_CHOICES))
        fk.addRow("KMI（Android/内核）", self.ks_kmi)
        vk.addLayout(fk)
        note_ks = QLabel(
            "内核级 Root：KernelSU/Next 用内置内核模块；SukiSU 需联网下载对应 KMI 的模块。"
            "产物用 fastboot 刷入 init_boot/boot。",
            tab_ks,
        )
        note_ks.setWordWrap(True)
        note_ks.setStyleSheet("color:#888;")
        vk.addWidget(note_ks)
        rowk = QHBoxLayout()
        self.ks_out = QLineEdit(tab_ks)
        bk = QPushButton("输出目录…", tab_ks)
        bk.clicked.connect(lambda: self._set_outdir(self.ks_out))
        rowk.addWidget(QLabel("输出目录", tab_ks))
        rowk.addWidget(self.ks_out, 1)
        rowk.addWidget(bk)
        vk.addLayout(rowk)
        self.ks_btn = QPushButton("开始修补", tab_ks)
        self.ks_btn.clicked.connect(self._ksud_patch)
        vk.addWidget(self.ks_btn)
        vk.addStretch(1)
        self.tabs.addTab(tab_ks, "KernelSU / SukiSU")

        # 共享日志
        self.log = QListWidget(self)
        self.log.setStyleSheet("QListWidget::item { font-family: Menlo; font-size: 11px; }")
        lay.addWidget(self.log, 1)
        clearbar = QHBoxLayout()
        clearbar.addStretch(1)
        clear_btn = QPushButton("清空日志", self)
        clear_btn.clicked.connect(lambda: self.log.clear())
        clearbar.addWidget(clear_btn)
        lay.addLayout(clearbar)

    # ---------- helpers ----------
    def _log(self, text: str):
        self.log.addItem(text)

    def _pick_file(self, edit: QLineEdit, title: str, filt: str):
        path, _ = QFileDialog.getOpenFileName(self, f"选择{title}", os.path.expanduser("~/Desktop"), filt)
        if path:
            edit.setText(path)

    def _pick_outdir(self):
        self._set_outdir(self.mag_out)

    def _set_outdir(self, edit: QLineEdit):
        path = QFileDialog.getExistingDirectory(self, "选择输出目录", os.path.expanduser("~/Desktop"))
        if path:
            edit.setText(path)

    def _default_out(self, edit: QLineEdit) -> str:
        out = (edit.text() or "").strip() or os.path.join(_SAVE_DIR, "offline_patch")
        os.makedirs(out, exist_ok=True)
        return out

    def _run(self, fn, on_done, disable_btn: QPushButton):
        if self._worker and self._worker.isRunning():
            return
        disable_btn.setEnabled(False)
        self.log.clear()
        self._worker = FastbootWorker(fn, self)
        self._worker.line.connect(self._log)
        self._worker.done.connect(lambda r: (disable_btn.setEnabled(True), on_done(r[0]))[1])
        self._worker.fail.connect(lambda m: (disable_btn.setEnabled(True), self._log(f"X 失败: {m}"))[1])
        self._worker.start()

    # ---------- Magisk ----------
    def _magisk_patch(self):
        from . import offline_patch

        boot = self.mag_boot.text().strip()
        apk = self.mag_apk.text().strip()
        if not os.path.isfile(boot):
            self._log("请选择 boot/init_boot 镜像")
            return
        if not os.path.isfile(apk):
            self._log("请选择 Magisk APK")
            return
        out = self._default_out(self.mag_out)
        options = {
            "abi": self.mag_abi.currentText(),
            "keep_verity": self.opt_keepverity.isChecked(),
            "keep_force_encrypt": self.opt_keeplen.isChecked(),
            "recovery": self.opt_recovery.isChecked(),
            "patch_vbmeta": self.opt_patchvbmeta.isChecked(),
            "force_rootfs": self.opt_forcefs.isChecked(),
        }

        def work(cb, ui):
            cb("开始 Magisk 修补...")
            return offline_patch.patch_magisk(boot, apk, out, options=options, line_cb=cb)

        self._run(work, lambda p: self._log(f"OK 修补完成: {p}"), self.mag_btn)

    # ---------- GKI ----------
    def _gki_build(self):
        from . import offline_patch

        boot = self.gki_boot.text().strip()
        ak3 = self.gki_ak3.text().strip()
        if not os.path.isfile(boot):
            self._log("请选择 boot.img")
            return
        if not os.path.isfile(ak3):
            self._log("请选择 AK3 压缩包")
            return
        out = self._default_out(self.gki_out)

        def work(cb, ui):
            cb("开始制作 GKI 镜像...")
            return offline_patch.build_gki(boot, ak3, out, line_cb=cb)

        self._run(work, lambda p: self._log(f"OK 制作完成: {p}"), self.gki_btn)

    # ---------- KernelSU / SukiSU ----------
    def _ksud_patch(self):
        boot = self.ks_boot.text().strip()
        if not os.path.isfile(boot):
            self._log("请选择 boot/init_boot 镜像")
            return
        method = self.ks_method.currentText()
        kmi = self.ks_kmi.currentText()
        if kmi == "自动检测":
            kmi = ""
        out = self._default_out(self.ks_out)

        def work(cb, ui):
            cb(f"开始 {method} 修补...")
            return offline_patch.patch_ksud(boot, method, out, kmi=kmi or None, line_cb=cb)

        self._run(work, lambda p: self._log(f"OK 修补完成: {p}"), self.ks_btn)
