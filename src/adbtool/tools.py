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
            "手机: 开发者选项 → 无线调试 → 使用配对码配对设备\n"
            "填入弹出的 IP:端口 和 6 位配对码后点“配对”",
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


_CACHE_DIR = os.path.join(
    os.path.expanduser("~/Library/Application Support"), "adbtool", "pkg_cache"
)
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
                rest = line[len("application-icon-"):]
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
                if n.lower().endswith(".png")
                and ("ic_launcher" in n.lower() or "/mipmap" in n.lower())
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
                    if (
                        data[:4] == b"\x89PNG"
                        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
                    ) and len(data) > best[0]:
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

    def __init__(
        self, adb: AdbClient, packages: list[str], parent=None, system_only: bool = False
    ):
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
                    ex.submit(
                        self._process_one, pkg, aapt, paths.get(pkg, ""), tmpdir, cdir
                    ): pkg
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
            tmp = os.path.join(
                tmpdir, hashlib.md5(pkg.encode()).hexdigest() + ".apk"
            )
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
        self._enrich.progress.connect(
            lambda i, t: self.status.setText(f"正在加载应用信息 {i}/{t}...")
        )
        self._enrich.finished.connect(self._on_enrich_done)
        self._enrich.start()

    def _on_tab_changed(self, idx: int):
        if idx == 1 and not self._sys_enriched:
            self._sys_enriched = True
            pkgs = sorted(self._list_pkgs.get(self.sys_list, ()))
            self._enrich_sys = EnrichWorker(
                self.adb, pkgs, self, system_only=True
            )
            self._enrich_sys.updated.connect(self._on_enrich)
            self._enrich_sys.progress.connect(
                lambda i, t: self.status.setText(
                    f"正在加载系统应用信息 {i}/{t}..."
                )
            )
            self._enrich_sys.finished.connect(self._on_enrich_done)
            self._enrich_sys.start()

    def _on_enrich_done(self):
        others = [
            w
            for w in (self._enrich, self._enrich_sys)
            if w is not None and w is not self.sender() and w.isRunning()
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
        item.setIcon(
            QApplication.style().standardIcon(
                QStyle.StandardPixmap.SP_FileIcon
            )
        )

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
        if (
            QMessageBox.question(self, "卸载", f"确定卸载 {pkg} 吗？")
            != QMessageBox.StandardButton.Yes
        ):
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
        if (
            QMessageBox.question(self, "清数据", f"确定清除 {pkg} 的所有数据吗？")
            != QMessageBox.StandardButton.Yes
        ):
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
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
            )
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
        self._rows.append(
            {"time": t, "pid": pid, "tid": tid, "level": lv, "tag": tag, "msg": msg}
        )

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
            text = (
                f"{r['time']} {r['pid']} {r['level']}/{r['tag']}: {r['msg']}"
            )
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
        default = os.path.join(
            _SAVE_DIR, f"logcat_{datetime.now():%Y%m%d_%H%M%S}.txt"
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "导出日志", default, "文本文件 (*.txt)"
        )
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
                    f"已保存到:\n{dest}\n\n"
                    "该文件为流式 MP4，若播放异常，可安装 ffmpeg 或使用 VLC 播放。",
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
        self._t.fail.connect(
            lambda m, c=cb: self._on_toggle_fail(c, m)
        )
        self._t.start()

    def _on_toggle_done(self, cb: QCheckBox):
        self._busy.discard(cb)
        cb.setEnabled(True)
        self.status.setText(
            f"已{'开启' if cb.isChecked() else '关闭'}"
        )

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
        self._apply_toggle(
            self.stay, lambda: self.adb.set_stay_awake(on), "充电常亮"
        )

    def _apply_scale(self):
        self.status.setText("正在设置动画缩放...")
        self._t = AdbTask(
            lambda: self.adb.set_anim_scale(self.scale.value()), (), self
        )
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
        self.quick_combo.addItems(
            ["boot", "boot_a", "boot_b", "init_boot", "init_boot_a",
             "recovery", "vendor_boot", "vendor_boot_a", "dtbo",
             "vbmeta", "vbmeta_system", "modem", "system", "vendor", "super"]
        )
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
        self.part_browser.setHorizontalHeaderLabels(
            ["", "分区名", "镜像文件", "选择镜像"]
        )
        self.part_browser.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.part_browser.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.part_browser.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self.part_browser.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
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
        self.part_table.setHorizontalHeaderLabels(
            ["刷入", "分区", "镜像文件", "操作", "进度"]
        )
        self.part_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.part_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.part_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self.part_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        self.part_table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Fixed
        )
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

        hint = QLabel(
            "选择完整刷机包（解压目录 / 卡刷 zip / payload.bin），解析后一键刷入。", tab_pkg
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#888;")
        vk.addWidget(hint)

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

        # ---- 标签③：设备与重启 ----
        tab_dev = QWidget(self)
        vd = QVBoxLayout(tab_dev)
        dev_hint = QLabel("设备维护操作：重启到指定模式、继续开机引导。", tab_dev)
        dev_hint.setStyleSheet("color:#888;")
        vd.addWidget(dev_hint)
        grp3 = QHBoxLayout()
        self.reboot_combo = QComboBox(tab_dev)
        self.reboot_combo.addItems(
            ["重启到系统", "重启到 Recovery", "重启到 FastbootD", "重启回 Bootloader", "继续启动(continue)"]
        )
        self.reboot_btn = QPushButton("执行重启", tab_dev)
        self.reboot_btn.clicked.connect(self._fb_reboot)
        grp3.addWidget(QLabel("重启", tab_dev))
        grp3.addWidget(self.reboot_combo, 1)
        grp3.addWidget(self.reboot_btn)
        vd.addLayout(grp3)
        vd.addStretch(1)
        self.tabs.addTab(tab_dev, "设备与重启")

        # 底部固定：输出区 + 状态
        self.output = QListWidget(self)
        self.output.setStyleSheet("QListWidget::item { font-family: Menlo; font-size: 11px; }")
        lay.addWidget(self.output, 1)

        self.status = QLabel("", self)
        lay.addWidget(self.status)
        self._refresh()

    def _refresh(self):
        self.refresh_btn.setEnabled(False)
        self.status.setText("正在检测 fastboot 设备...")
        self._worker = FastbootWorker(
            lambda cb, ui: self._do_refresh(cb), self
        )
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
        path = QFileDialog.getExistingDirectory(
            self, "选择刷机包目录", os.path.expanduser("~/Downloads")
        )
        if path:
            self.pkg_edit.setText(path)

    def _browse_pkg_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择卡刷包", os.path.expanduser("~/Downloads"),
            "卡刷包 (*.zip *.bin);;所有文件 (*)"
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
        self._cmds = result["commands"]
        self.pkg_preview.clear()
        for i, c in enumerate(self._cmds, 1):
            self.pkg_preview.addItem(f"[{i}/{len(self._cmds)}] {c['raw']}")
        self.part_table.setRowCount(0)
        seen = set()
        for c in result["commands"]:
            args = c["args"]
            if "flash" not in args:
                continue
            fi = args.index("flash")
            img = ""
            for a in args[fi + 1:]:
                if a.endswith(
                    (".img", ".img.zst", ".lz4", ".zip", ".dat",
                     ".elf", ".melf", ".mbn", ".bin", ".fv", ".txt")
                ):
                    img = a
            part = ""
            for a in args[fi + 1:]:
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
        ab = "A/B 双槽" if result["ab"] else "A-only"
        self.status.setText(
            f"已解析刷机包（{ab}）{len(self._cmds)} 条命令 / {len(seen)} 个分区"
            + (f"，机型: {rd}" if rd else "")
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
        ck.setFlags(
            Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
        )
        ck.setCheckState(
            Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        )
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
        rows = sorted(
            {i.row() for i in self.part_table.selectedItems()}, reverse=True
        )
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
            self, "选择镜像", os.path.expanduser("~/Desktop"),
            "镜像文件 (*.img *.img.zst *.lz4);;所有文件 (*)"
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
            self, "选择镜像", os.path.expanduser("~/Desktop"),
            "镜像文件 (*.img *.img.zst *.lz4);;所有文件 (*)"
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
            self, "选择镜像文件",
            os.path.expanduser("~/Desktop"),
            "镜像文件 (*.img *.img.zst *.lz4);;所有文件 (*)"
        )
        if not files:
            return
        count = 0
        for f in files:
            name = os.path.basename(f)
            part = name.lower()
            for suffix in (".img", ".img.zst", ".lz4"):
                if part.endswith(suffix):
                    part = part[:-len(suffix)]
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
        msg = (
            f"将按顺序刷入 {len(rows)} 个分区：\n\n"
            + "\n".join(f"  {p} ← {os.path.basename(i)}" for r, p, i in rows)
        )
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
            self, "确认完整刷入", msg,
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
        self._worker.done.connect(lambda _: (self.reboot_btn.setEnabled(True), self.status.setText(f"OK 已发送重启指令（{action}）"))[1])
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
