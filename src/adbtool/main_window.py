from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .adb_client import AdbClient, AdbError
from .env_check import ensure_adb
from .file_panel import FilePanel
from .tools import (
    AdbTask,
    FastbootDialog,
    LogcatDialog,
    PackageDialog,
    PairDialog,
    RecordDialog,
    ToggleDialog,
)


class TransferWorker(QThread):
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, kind: str, paths: list[str], dest: str, adb: AdbClient, parent=None):
        super().__init__(parent)
        self.kind = kind  # "push" | "pull"
        self.paths = paths
        self.dest = dest
        self.adb = adb

    def run(self):
        try:
            for p in self.paths:
                if self.kind == "push":
                    self.adb.push(p, os.path.join(self.dest, os.path.basename(p)))
                else:
                    self.adb.pull(p, os.path.join(self.dest, os.path.basename(p)))
            self.finished_ok.emit(
                f"传输到{'手机' if self.kind == 'push' else '电脑'}完成: {len(self.paths)} 项"
            )
        except AdbError as e:
            self.failed.emit(str(e))


class InstallWorker(QThread):
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, apks: list[str], adb: AdbClient, parent=None):
        super().__init__(parent)
        self.apks = apks
        self.adb = adb

    def run(self):
        try:
            self.adb.install_apk(self.apks)
            self.finished_ok.emit(f"安装完成: {len(self.apks)} 个应用")
        except AdbError as e:
            self.failed.emit(str(e))


class BrewWorker(QThread):
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, formula: str, parent=None):
        super().__init__(parent)
        self.formula = formula

    def run(self):
        try:
            proc = subprocess.run(
                ["brew", "install", self.formula],
                capture_output=True,
                text=True,
                timeout=1800,
            )
            if proc.returncode != 0:
                self.failed.emit(proc.stderr.strip() or proc.stdout.strip())
            else:
                self.finished_ok.emit(self.formula)
        except Exception as e:
            self.failed.emit(str(e))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ADB 文件管理器")
        self.resize(1180, 700)
        self.adb = self._create_adb()
        self._worker: TransferWorker | None = None

        self._build_ui()
        self._init_local_panel()
        self._refresh_devices()

    def _create_adb(self) -> AdbClient:
        try:
            return AdbClient()
        except AdbError:
            ret = QMessageBox.question(
                self,
                "缺少 adb",
                "未找到 Android 调试工具 adb，本软件需要它来操作手机。\n"
                "是否自动下载并安装？\n（官方源优先，失败自动切换国内镜像）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if ret != QMessageBox.StandardButton.Yes:
                QMessageBox.warning(
                    self,
                    "需要 adb",
                    "请手动安装 platform-tools：\n"
                    "1. 打开 https://developer.android.com/tools/releases/platform-tools\n"
                    "2. 下载 macOS 版并解压\n"
                    "3. 将 adb 放入 PATH 或安装到 ~/Library/Android/sdk/platform-tools/\n"
                    "然后重新打开本软件。",
                )
                sys.exit(1)
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                path = ensure_adb(lambda m: print(f"[adb] {m}"))
                return AdbClient(path)
            except AdbError as e:
                QMessageBox.critical(
                    self,
                    "下载失败",
                    f"自动下载 adb 失败：{e}\n\n"
                    "请手动安装 platform-tools：\n"
                    "https://developer.android.com/tools/releases/platform-tools",
                )
                sys.exit(1)
            finally:
                QApplication.restoreOverrideCursor()

    def _init_local_panel(self):
        last = self.local_panel.saved_path()
        if last and os.path.isdir(last):
            self.local_panel.navigate(last)
        else:
            self.local_panel.navigate(str(Path.home()))

    def _build_ui(self):
        self._build_toolbar()
        self._build_menubar()

        self.phone_panel = FilePanel(is_local=False, adb=self.adb, parent=self)
        self.local_panel = FilePanel(is_local=True, adb=self.adb, parent=self)
        self.phone_panel.setObjectName("phone")
        self.local_panel.setObjectName("local")

        self._build_device_info()

        self.splitter = QSplitter(self)
        self.splitter.addWidget(self._wrap_panel("手机", self.phone_panel))
        self.splitter.addWidget(self._wrap_panel("电脑", self.local_panel))
        self.splitter.setSizes([590, 590])

        central = QWidget(self)
        central_lay = QVBoxLayout(central)
        central_lay.setContentsMargins(0, 0, 0, 0)
        central_lay.setSpacing(0)
        central_lay.addWidget(self.device_info_frame, 0)
        central_lay.addWidget(self.splitter, 1)
        self.setCentralWidget(central)

        self.phone_panel.transfer_requested.connect(
            lambda paths, _: self._start_transfer("push", paths, self.phone_panel.get_cwd())
        )
        self.local_panel.transfer_requested.connect(
            lambda paths, _: self._start_transfer("push", paths, self.phone_panel.get_cwd())
        )
        self.phone_panel.download_requested.connect(
            lambda paths, _: self._start_transfer("pull", paths, self.local_panel.get_cwd())
        )
        self.local_panel.download_requested.connect(
            lambda paths, _: self._start_transfer("pull", paths, self.local_panel.get_cwd())
        )
        self.local_panel.install_requested.connect(self._install_apks)
        self.phone_panel.status_message.connect(self.statusBar().showMessage)
        self.local_panel.status_message.connect(self.statusBar().showMessage)
        self.statusBar().showMessage("就绪")

    # ---------- chrome ----------

    def _build_device_info(self):
        self.device_info_frame = QFrame(self)
        self.device_info_frame.setFrameShape(QFrame.Shape.StyledPanel)
        self.device_info_frame.setStyleSheet(
            "QFrame { background: #2b2b2b; border-bottom: 1px solid #444; }"
        )
        self.device_info_frame.setMaximumHeight(120)
        self.device_info_labels = {}
        layout = QHBoxLayout(self.device_info_frame)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(30)

        fields = [
            ("model", "型号"),
            ("android_version", "安卓版本"),
            ("battery", "电池"),
            ("storage", "存储"),
            ("resolution", "分辨率"),
            ("security_patch", "安全补丁"),
        ]
        grid = QGridLayout()
        grid.setSpacing(8)
        grid.setVerticalSpacing(10)
        for i, (key, label_text) in enumerate(fields):
            row, col = divmod(i, 3)
            lbl = QLabel(f"{label_text}:", self.device_info_frame)
            lbl.setStyleSheet("color: #999; font-size: 13px;")
            val = QLabel("--", self.device_info_frame)
            val.setStyleSheet("color: #ddd; font-size: 13px;")
            val.setMinimumWidth(140)
            grid.addWidget(lbl, row, col * 2)
            grid.addWidget(val, row, col * 2 + 1)
            self.device_info_labels[key] = val
        layout.addLayout(grid)
        layout.addStretch(1)

        no_device = QLabel("未连接设备", self.device_info_frame)
        no_device.setStyleSheet("color: #666; font-size: 13px;")
        self.device_info_labels["_no_device"] = no_device
        layout.addWidget(no_device)

    def _update_device_info(self, info):
        if not info or not info.serial:
            self.device_info_frame.setMaximumHeight(40)
            for k, lbl in self.device_info_labels.items():
                if k == "_no_device":
                    lbl.show()
                else:
                    lbl.setText("--")
            return
        self.device_info_frame.setMaximumHeight(120)
        self.device_info_labels["_no_device"].hide()

        name = info.market_name or info.model or info.serial
        if info.code:
            name += f" ({info.code})"
        self.device_info_labels["model"].setText(name)

        ver = info.android_version
        if info.android_sdk:
            ver += f" (SDK {info.android_sdk})"
        if info.miui_version:
            ver += f" | {info.miui_version}"
        self.device_info_labels["android_version"].setText(ver or "--")

        bat = info.battery_level or "--"
        if info.battery_status:
            bat += f" {info.battery_status}"
        self.device_info_labels["battery"].setText(bat)

        if info.storage_total:
            if info.storage_free and info.storage_total:
                self.device_info_labels["storage"].setText(
                    f"{info.storage_free} 可用 / {info.storage_total}"
                )
            else:
                self.device_info_labels["storage"].setText(info.storage_total)
        else:
            self.device_info_labels["storage"].setText("--")

        self.device_info_labels["resolution"].setText(info.resolution or "--")
        self.device_info_labels["security_patch"].setText(info.security_patch or "--")

    def _reboot_default(self):
        self._reboot_device("system")

    def _wrap_panel(self, title: str, panel: FilePanel) -> QWidget:
        panel.set_panel_title(title)
        return panel

    def _build_toolbar(self):
        tb = QToolBar("工具栏", self)
        tb.setMovable(False)
        self.addToolBar(tb)

        tb.addWidget(QLabel(" 设备: ", self))
        self.device_combo = QComboBox(self)
        self.device_combo.setMinimumWidth(200)
        self.device_combo.currentIndexChanged.connect(self._on_device_selected)
        tb.addWidget(self.device_combo)
        self.refresh_devices_btn = QPushButton("刷新", self)
        self.refresh_devices_btn.setToolTip("刷新设备列表")
        self.refresh_devices_btn.clicked.connect(self._refresh_devices)
        tb.addWidget(self.refresh_devices_btn)

        tb.addSeparator()
        self.mirror_btn = QPushButton("投屏", self)
        self.mirror_btn.setToolTip("把手机屏幕投到电脑（scrcpy）")
        self.mirror_btn.clicked.connect(self._open_mirror)
        tb.addWidget(self.mirror_btn)
        self.scrshot_btn = QPushButton("截屏", self)
        self.scrshot_btn.setToolTip("截取手机屏幕保存到桌面")
        self.scrshot_btn.clicked.connect(self._screenshot)
        tb.addWidget(self.scrshot_btn)
        self.record_btn = QPushButton("录屏", self)
        self.record_btn.setToolTip("录制手机屏幕")
        self.record_btn.clicked.connect(self._show_record)
        tb.addWidget(self.record_btn)
        self.packages_btn = QPushButton("应用", self)
        self.packages_btn.setToolTip("管理手机应用：卸载/启动/停止/清数据")
        self.packages_btn.clicked.connect(self._show_packages)
        tb.addWidget(self.packages_btn)
        self.logcat_btn = QPushButton("日志", self)
        self.logcat_btn.setToolTip("实时查看手机日志")
        self.logcat_btn.clicked.connect(self._show_logcat)
        tb.addWidget(self.logcat_btn)
        self.pair_btn = QPushButton("配对", self)
        self.pair_btn.setToolTip("无线 ADB 配对连接")
        self.pair_btn.clicked.connect(self._show_pair)
        tb.addWidget(self.pair_btn)
        self.toggle_btn = QPushButton("开关", self)
        self.toggle_btn.setToolTip("Wi-Fi/蓝牙/充电常亮/动画缩放")
        self.toggle_btn.clicked.connect(self._show_toggles)
        tb.addWidget(self.toggle_btn)
        self.fastboot_btn = QPushButton("刷机", self)
        self.fastboot_btn.setToolTip("Fastboot 刷机：单分区/完整包刷写")
        self.fastboot_btn.clicked.connect(self._show_fastboot)
        tb.addWidget(self.fastboot_btn)

        tb.addSeparator()
        self.reboot_btn = QPushButton("重启", self)
        self.reboot_btn.setToolTip("重启设备（点击选择模式）")
        self.reboot_menu = QMenu(self)
        for text, mode in (
            ("重启系统", "system"),
            ("重启到 Recovery", "recovery"),
            ("重启到 Fastboot", "bootloader"),
            ("重启到 FastbootD", "fastboot"),
        ):
            act = self.reboot_menu.addAction(text)
            act.triggered.connect(lambda _, m=mode: self._reboot_device(m))
        self.reboot_btn.setMenu(self.reboot_menu)
        self.reboot_btn.clicked.connect(self._reboot_default)
        tb.addWidget(self.reboot_btn)

        tb.addSeparator()
        self.swap_btn = QPushButton("⇄ 交换", self)
        self.swap_btn.setToolTip("左右两栏互换位置")
        self.swap_btn.clicked.connect(self._swap_panels)
        tb.addWidget(self.swap_btn)

    def _build_menubar(self):
        m = self.menuBar()
        view = m.addMenu("视图")
        swap_act = view.addAction("⇄ 交换栏")
        swap_act.triggered.connect(self._swap_panels)
        hidden_act = view.addAction("显示/隐藏隐藏文件")
        hidden_act.setCheckable(True)
        hidden_act.triggered.connect(self._set_hidden)
        view.addSeparator()
        view.addAction("刷新").triggered.connect(self._refresh_all)
        tool = m.addMenu("工具")
        tool.addAction("无线 ADB 配对").triggered.connect(self._show_pair)
        tool.addAction("应用管理").triggered.connect(self._show_packages)
        tool.addAction("Logcat 日志").triggered.connect(self._show_logcat)
        tool.addAction("截屏").triggered.connect(self._screenshot)
        tool.addAction("屏幕录制").triggered.connect(self._show_record)
        tool.addAction("系统开关").triggered.connect(self._show_toggles)
        tool.addAction("Fastboot 刷机").triggered.connect(self._show_fastboot)
        reboot_menu = tool.addMenu("重启设备")
        for text, mode in (
            ("重启系统", "system"),
            ("重启到 Recovery", "recovery"),
            ("重启到 Fastboot", "bootloader"),
            ("重启到 FastbootD", "fastboot"),
        ):
            act = reboot_menu.addAction(text)
            act.triggered.connect(lambda _, m=mode: self._reboot_device(m))
        help_menu = m.addMenu("帮助")
        help_menu.addAction("操作说明").triggered.connect(self._show_help)

    # ---------- device ----------

    def _refresh_devices(self):
        self.statusBar().showMessage("正在刷新设备...")
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        try:
            devices = self.adb.list_devices()
        except AdbError as e:
            self.statusBar().showMessage(f"adb 错误: {e}")
            return
        available = [d for d in devices if d.state == "device"]
        for d in available:
            self.adb.device = d.serial
            try:
                info = self.adb.get_device_info()
                d.market_name = info.market_name
                d.code = info.code
                if not d.model:
                    d.model = info.model
            except AdbError:
                pass
            if d.market_name:
                label = f"{d.market_name} ({d.code})" if d.code else d.market_name
            else:
                label = f"{d.serial} ({d.model})" if d.model else d.serial
            row = self.device_combo.count()
            self.device_combo.addItem(label, d.serial)
            self.device_combo.setItemData(row, d.serial, Qt.ItemDataRole.ToolTipRole)
        self.device_combo.blockSignals(False)
        if available:
            self._on_device_selected(0)
            self.statusBar().showMessage(f"检测到 {len(available)} 台设备")
        else:
            if self.adb.device:
                self.adb.device = ""
                self.phone_panel.clear_content()
            if devices:
                self.statusBar().showMessage(
                    f"{len(devices)} 台设备离线（可能已断开连接），请重新连接后刷新"
                )
            else:
                self.statusBar().showMessage(
                    "未检测到设备：请确认手机开启 USB 调试、已授权，无线连接时手机勿锁屏"
                )

    def _on_device_selected(self, index: int):
        if index < 0:
            return
        serial = self.device_combo.itemData(index)
        if not serial:
            return
        self.adb.device = serial
        try:
            root = self.adb.get_storage_root()
        except AdbError as e:
            self.statusBar().showMessage(f"无法打开存储: {e}")
            return
        last = self.phone_panel.saved_path()
        if last:
            self.phone_panel.navigate(last)
            if self.phone_panel.current_path != last:
                self.phone_panel.navigate(root)
        else:
            self.phone_panel.navigate(root)
        self.statusBar().showMessage(f"已连接 {serial}")
        try:
            info = self.adb.get_device_info()
            self._update_device_info(info)
        except Exception:
            self._update_device_info(None)

    # ---------- view actions ----------

    def _swap_panels(self):
        w0 = self.splitter.widget(0)
        w1 = self.splitter.widget(1)
        self.splitter.insertWidget(0, w1)
        self.splitter.insertWidget(1, w0)
        self.splitter.setSizes([590, 590])

    def _set_hidden(self, on: bool):
        self.phone_panel.hidden_check.setChecked(on)
        self.local_panel.hidden_check.setChecked(on)

    def _refresh_all(self):
        self.phone_panel.refresh()
        self.local_panel.refresh()

    # ---------- transfer ----------

    def _start_transfer(self, kind: str, paths: list[str], dest: str):
        if not paths or not dest:
            self.statusBar().showMessage("请先选择文件")
            return
        if self._worker and self._worker.isRunning():
            self.statusBar().showMessage("已有传输任务进行中，请稍候")
            return
        self._worker = TransferWorker(kind, paths, dest, self.adb, self)
        target = self.phone_panel if kind == "push" else self.local_panel
        self._worker.finished_ok.connect(lambda m: self.statusBar().showMessage(m))
        self._worker.finished_ok.connect(lambda _: target.refresh())
        self._worker.failed.connect(lambda m: self.statusBar().showMessage(f"传输失败: {m}"))
        self._worker.start()
        self.statusBar().showMessage(
            f"正在传输到{'手机' if kind == 'push' else '电脑'}..."
        )

    def _install_apks(self, apks: list[str]):
        if not self.adb.device:
            self.statusBar().showMessage("请先连接手机")
            return
        if self._worker and self._worker.isRunning():
            self.statusBar().showMessage("已有任务进行中，请稍候")
            return
        self._worker = InstallWorker(apks, self.adb, self)
        self._worker.finished_ok.connect(lambda m: self.statusBar().showMessage(m))
        self._worker.failed.connect(lambda m: self.statusBar().showMessage(f"安装失败: {m}"))
        self._worker.start()
        self.statusBar().showMessage(f"正在安装 {len(apks)} 个应用...")

    # ---------- mirror ----------

    def _open_mirror(self):
        if not self.adb.device:
            self.statusBar().showMessage("请先连接手机")
            return
        if self._worker and self._worker.isRunning():
            self.statusBar().showMessage("已有任务进行中，请稍候")
            return
        if shutil.which("scrcpy"):
            self._launch_scrcpy()
            return
        ret = QMessageBox.question(
            self,
            "需要 scrcpy",
            "未找到投屏工具 scrcpy。\n是否自动用 Homebrew 安装？（可能需要几分钟）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if ret != QMessageBox.StandardButton.Yes:
            QMessageBox.information(
                self, "安装 scrcpy", "可手动执行：brew install scrcpy"
            )
            return
        self._worker = BrewWorker("scrcpy", self)
        self._worker.finished_ok.connect(
            lambda _: (
                self.statusBar().showMessage("scrcpy 安装完成，正在启动投屏..."),
                self._launch_scrcpy(),
            )
        )
        self._worker.failed.connect(
            lambda m: self.statusBar().showMessage(f"安装 scrcpy 失败: {m}")
        )
        self._worker.start()
        self.statusBar().showMessage("正在安装 scrcpy（可能需要几分钟）...")

    def _launch_scrcpy(self):
        try:
            subprocess.Popen(["scrcpy", "-s", self.adb.device])
            self.statusBar().showMessage("投屏窗口已启动")
        except OSError as e:
            self.statusBar().showMessage(f"启动投屏失败: {e}")

    # ---------- tools ----------

    def _show_pair(self):
        dlg = PairDialog(self.adb, self)
        dlg.paired.connect(self._refresh_devices)
        dlg.exec()

    def _show_packages(self):
        if not self.adb.device:
            self.statusBar().showMessage("请先连接手机")
            return
        PackageDialog(self.adb, self).exec()

    def _show_logcat(self):
        if not self.adb.device:
            self.statusBar().showMessage("请先连接手机")
            return
        LogcatDialog(self.adb, self).show()

    def _show_record(self):
        if not self.adb.device:
            self.statusBar().showMessage("请先连接手机")
            return
        RecordDialog(self.adb, self).exec()

    def _show_toggles(self):
        if not self.adb.device:
            self.statusBar().showMessage("请先连接手机")
            return
        ToggleDialog(self.adb, self).exec()

    def _show_fastboot(self):
        FastbootDialog(self.adb, self).exec()

    def _reboot_device(self, mode: str):
        if not self.adb.device:
            self.statusBar().showMessage("请先连接手机")
            return
        names = {
            "system": "系统",
            "recovery": "Recovery",
            "bootloader": "Fastboot",
            "fastboot": "FastbootD",
        }
        ret = QMessageBox.question(
            self,
            "确认重启",
            f"确定要重启手机到 {names.get(mode, mode)} 吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        self.statusBar().showMessage(f"正在重启到 {names.get(mode, mode)} ...")
        self._reboot_task = AdbTask(
            lambda: self.adb.reboot(mode), (), self
        )
        self._reboot_task.done.connect(
            lambda _: self.statusBar().showMessage("已发送重启指令")
        )
        self._reboot_task.fail.connect(
            lambda m: self.statusBar().showMessage(f"重启失败: {m}")
        )
        self._reboot_task.start()

    def _screenshot(self):
        if not self.adb.device:
            self.statusBar().showMessage("请先连接手机")
            return
        if self._worker and self._worker.isRunning():
            self.statusBar().showMessage("已有任务进行中，请稍候")
            return
        dest = os.path.join(
            str(Path.home() / "Desktop"),
            f"截图_{datetime.now():%Y%m%d_%H%M%S}.png",
        )
        self.statusBar().showMessage("正在截屏...")
        self._worker = AdbTask(self.adb.screenshot, (dest,), self)
        self._worker.done.connect(
            lambda _: self.statusBar().showMessage(f"截屏已保存: {dest}")
        )
        self._worker.fail.connect(
            lambda m: self.statusBar().showMessage(f"截屏失败: {m}")
        )
        self._worker.start()

    def _show_help(self):
        QMessageBox.information(
            self,
            "操作说明",
            "导航\n"
            "  双击文件夹进入；双击文件在系统/手机打开\n"
            "  后退 ◀ / 前进 ▶ / 上级 ▲；地址栏可输入路径直接跳转\n\n"
            "操作(面板按钮或右键菜单)\n"
            "  新建/重命名/剪切/复制/粘贴/删除/刷新\n"
            "  传输到手机 / 传输到电脑\n"
            "  视图切换：列表 / 宫格\n\n"
            "拖拽\n"
            "  按住文件拖到另一侧松开 = 传输；拖到同侧 = 移动\n\n"
            "快捷键\n"
            "  Delete 删除 · F2 重命名 · Ctrl+C/X/V 复制剪切粘贴\n"
            "  Enter 打开 · Backspace 上级 · F5 刷新\n\n"
            "工具栏\n"
            "  ⇄ 交换左右栏 · 显示隐藏文件\n"
            "  面板列宽可拖动调整，自动记忆；视图模式也会记住",
        )