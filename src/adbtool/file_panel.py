from __future__ import annotations

import os
import shlex
from datetime import datetime
from pathlib import Path, PurePosixPath

from PyQt6.QtCore import QMimeData, QSettings, QSize, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QDesktopServices,
    QDrag,
    QKeySequence,
    QShortcut,
    QStandardItem,
    QStandardItemModel,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFileIconProvider,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .adb_client import AdbClient, AdbError, FileEntry

ROLE_IS_DIR = Qt.ItemDataRole.UserRole + 1
ROLE_PATH = Qt.ItemDataRole.UserRole
ROLE_SORT = Qt.ItemDataRole.UserRole + 2

MIME_DROP = "application/x-adbtool-drop"


class DropModel(QStandardItemModel):
    """QStandardItemModel with custom drag payload for cross-panel transfers."""

    def __init__(self, panel: FilePanel):
        super().__init__(0, 3, panel)
        self.panel = panel

    def mimeTypes(self) -> list[str]:
        return [MIME_DROP]

    def mimeData(self, indexes):
        rows = sorted({i.row() for i in indexes})
        paths = [self.item(r, 0).data(ROLE_PATH) for r in rows if self.item(r, 0)]
        if not paths:
            return None
        md = QMimeData()
        md.setData(
            MIME_DROP,
            (("local" if self.panel.is_local else "remote") + "\n" + "\n".join(paths)).encode(),
        )
        return md

    def supportedDropActions(self) -> Qt.DropAction:
        return Qt.DropAction.CopyAction | Qt.DropAction.MoveAction

    def flags(self, index):
        return super().flags(index) | Qt.ItemFlag.ItemIsDragEnabled

    def canDropMimeData(self, data, action, row, column, parent):
        return data.hasFormat(MIME_DROP)

    def dropMimeData(self, data, action, row, column, parent):
        if not data.hasFormat(MIME_DROP):
            return False
        raw = bytes(data.data(MIME_DROP)).decode()
        first, *rest = raw.split("\n")
        self.panel._on_drop(first == "local", [p for p in rest if p])
        return True


class _DragDropMixin:
    def __init__(self, panel: FilePanel):
        self.panel = panel
        self._press_pos = None
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDragEnabled(True)
        self.setDropIndicatorShown(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.viewport().setMouseTracking(True)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._press_pos = e.position().toPoint()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if (
            self._press_pos
            and (e.buttons() & Qt.MouseButton.LeftButton)
            and (e.position().toPoint() - self._press_pos).manhattanLength() >= QApplication.startDragDistance()
        ):
            self._press_pos = None
            self._start_drag()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._press_pos = None
        super().mouseReleaseEvent(e)

    def _start_drag(self):
        paths = self.panel.get_selected_paths()
        if not paths:
            return
        md = QMimeData()
        md.setData(
            MIME_DROP,
            (("local" if self.panel.is_local else "remote") + "\n" + "\n".join(paths)).encode(),
        )
        drag = QDrag(self)
        drag.setMimeData(md)
        rows = self.panel._all_selected_rows()
        if rows:
            icon = self.panel.model.item(rows[0], 0).icon()
            pm = icon.pixmap(48, 48)
            drag.setPixmap(pm)
            drag.setHotSpot(pm.rect().center())
        drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction)

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(MIME_DROP):
            e.acceptProposedAction()
            self.setStyleSheet("border: 2px solid #3daee9;")
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if e.mimeData().hasFormat(MIME_DROP):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragLeaveEvent(self, e):
        self.setStyleSheet("")
        super().dragLeaveEvent(e)

    def dropEvent(self, e):
        self.setStyleSheet("")
        md = e.mimeData()
        if not md.hasFormat(MIME_DROP):
            e.ignore()
            return
        raw = bytes(md.data(MIME_DROP)).decode()
        first, *rest = raw.split("\n")
        self.panel._on_drop(first == "local", [p for p in rest if p])
        e.acceptProposedAction()


class DragDropTableView(_DragDropMixin, QTableView):
    def __init__(self, panel, *args, **kwargs):
        QTableView.__init__(self, *args, **kwargs)
        _DragDropMixin.__init__(self, panel)


class DragDropListView(_DragDropMixin, QListView):
    def __init__(self, panel, *args, **kwargs):
        QListView.__init__(self, *args, **kwargs)
        _DragDropMixin.__init__(self, panel)


class FilePanel(QWidget):
    path_changed = pyqtSignal(str)
    status_message = pyqtSignal(str)
    transfer_requested = pyqtSignal(list, str)  # local -> phone (push)
    download_requested = pyqtSignal(list, str)  # phone -> local (pull)
    install_requested = pyqtSignal(list)  # local apk files -> phone

    _clipboard: tuple[bool, list[str], bool] | None = None

    def __init__(self, is_local: bool, adb: AdbClient, parent=None):
        super().__init__(parent)
        self.is_local = is_local
        self.adb = adb
        self.current_path = ""
        self._show_hidden = False
        self._back: list[str] = []
        self._fwd: list[str] = []
        self._settings = QSettings("adbtool", "adbtool")
        self._key = "local" if is_local else "phone"

        self.model = DropModel(self)
        self.model.setHorizontalHeaderLabels(["名称", "大小", "修改时间"])
        self._icons = QFileIconProvider()

        self._build_table()
        self._build_grid()
        self._build_nav_row()
        self._build_action_row()
        self.info_label = QLabel("", self)
        self.info_label.setStyleSheet("color: #666; padding: 2px 4px;")

        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.table)
        self.stack.addWidget(self.grid)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(3)
        layout.addLayout(self.nav_row)
        layout.addLayout(self.action_row)
        layout.addWidget(self.stack, 1)
        layout.addWidget(self.info_label)

        self._bind_shortcuts()
        self._seed_path_combo()
        self._restore_prefs()
        self._apply_drag_config()

    def _apply_drag_config(self):
        for v in (self.table, self.grid):
            v.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
            v.setDragEnabled(True)
            v.setDropIndicatorShown(True)
            v.setAcceptDrops(True)
            v.viewport().setAcceptDrops(True)
            v.viewport().setMouseTracking(True)

    # ---------- UI construction ----------

    def _build_table(self):
        self.table = DragDropTableView(self)
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.doubleClicked.connect(self._on_double_click)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self.update_status())
        self.table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        self.table.horizontalHeader().sectionResized.connect(lambda *_: self._save_widths())
        self._sort_col = 0
        self._sort_asc = True

    def _build_grid(self):
        self.grid = DragDropListView(self)
        self.grid.setModel(self.model)
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setIconSize(QSize(48, 48))
        self.grid.setGridSize(QSize(120, 96))
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setWordWrap(True)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.grid.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid.doubleClicked.connect(self._on_double_click)
        self.grid.customContextMenuRequested.connect(self._show_context_menu)
        self.grid.selectionModel().selectionChanged.connect(lambda *_: self.update_status())

    def set_panel_title(self, title: str):
        if self._title_label is not None:
            self._title_label.setText(f"  {title} ")
        else:
            self._title_label = QLabel(f"  {title} ", self)
            self._title_label.setStyleSheet("background: #2d2d2d; color: #fff; padding: 4px 8px; font-weight: bold;")
            self.nav_row.insertWidget(0, self._title_label)

    def _build_nav_row(self):
        self.nav_row = QHBoxLayout()
        self._title_label = None
        self.back_btn = self._icon_btn("◀", "后退", self.go_back)
        self.fwd_btn = self._icon_btn("▶", "前进", self.go_forward)
        self.up_btn = self._icon_btn("▲", "上级目录", self.go_up)
        if self.is_local:
            self.path_combo = None
            self.path_edit = QLineEdit(self)
            self.path_edit.setMinimumWidth(200)
            self.path_edit.setPlaceholderText("输入路径或点击选择目录...")
            self.path_edit.returnPressed.connect(self._jump_to_edit)
            self.browse_btn = QPushButton("选择目录...", self)
            self.browse_btn.clicked.connect(self._browse_local_dir)
        else:
            self.path_edit = None
            self.browse_btn = None
            self.path_combo = QComboBox(self)
            self.path_combo.setEditable(True)
            self.path_combo.setMinimumWidth(200)
            self.path_combo.lineEdit().returnPressed.connect(self._jump_to_path)
            self.path_combo.activated.connect(self._jump_to_path)
        self.refresh_btn = self._icon_btn("刷新", "刷新", self._do_refresh)
        self._refresh_normal_style = ""
        self._refresh_active_style = "color: #3daee9; font-weight: bold;"

        self.hidden_check = QCheckBox("隐藏文件", self)
        self.hidden_check.toggled.connect(self._toggle_hidden)
        self.list_btn = self._icon_btn("列表", "列表视图", self._view_list, checkable=True)
        self.grid_btn = self._icon_btn("宫格", "宫格(图标)视图", self._view_grid, checkable=True)
        for w in (self.back_btn, self.fwd_btn, self.up_btn):
            self.nav_row.addWidget(w)
        if self.is_local:
            self.nav_row.addWidget(self.path_edit, 1)
            self.nav_row.addWidget(self.browse_btn)
        else:
            self.nav_row.addWidget(self.path_combo, 1)
        self.nav_row.addWidget(self.refresh_btn)
        self.nav_row.addWidget(self.list_btn)
        self.nav_row.addWidget(self.grid_btn)
        self.nav_row.addWidget(self.hidden_check)

    def _build_action_row(self):
        self.action_row = QHBoxLayout()
        self.new_btn = self._icon_btn("＋ 新建", None, self.mkdir)
        self.rename_btn = self._icon_btn("重命名", None, self.rename)
        self.cut_btn = self._icon_btn("剪切", None, self.cut_selection)
        self.copy_btn = self._icon_btn("复制", None, self.copy_selection)
        self.paste_btn = self._icon_btn("粘贴", None, self.paste)
        self.del_btn = self._icon_btn("删除", "删除选中项 (Delete)", self.delete)
        for w in (self.new_btn, self.rename_btn, self.cut_btn, self.copy_btn, self.paste_btn, self.del_btn):
            self.action_row.addWidget(w)
        self.action_row.addStretch(1)
        if self.is_local:
            self.transfer_btn = self._icon_btn("传输到手机", None, self._to_phone)
            self.action_row.addWidget(self.transfer_btn)
            self.install_btn = self._icon_btn("安装到手机", "选中 .apk 文件后安装到手机", self._install_apks)
            self.action_row.addWidget(self.install_btn)
        else:
            self.transfer_btn = self._icon_btn("传输到电脑", None, self._to_computer)
            self.action_row.addWidget(self.transfer_btn)

    def _icon_btn(self, text, tip, slot, checkable=False):
        b = QPushButton(text, self)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setCheckable(checkable)
        if tip:
            b.setToolTip(tip)
        if slot:
            b.clicked.connect(slot)
        return b

    def _bind_shortcuts(self):
        self._bind(QKeySequence.StandardKey.Delete, self.delete)
        self._bind(QKeySequence.StandardKey.Copy, self.copy_selection)
        self._bind(QKeySequence.StandardKey.Cut, self.cut_selection)
        self._bind(QKeySequence.StandardKey.Paste, self.paste)
        self._bind(QKeySequence.StandardKey.Refresh, self.refresh)
        self._bind("F2", self.rename)
        self._bind("Backspace", self.go_up)
        self._bind("Return", self._enter)

    def _bind(self, seq, slot):
        sc = QShortcut(QKeySequence(seq), self)
        sc.setContext(Qt.ShortcutContext.WidgetShortcut)
        sc.activated.connect(slot)

    # ---------- prefs (view mode + column widths) ----------

    def _restore_prefs(self):
        widths = self._settings.value(f"{self._key}/col_widths")
        if isinstance(widths, list) and len(widths) == 3:
            self.table.setColumnWidth(0, int(widths[0]))
            self.table.setColumnWidth(1, int(widths[1]))
            self.table.setColumnWidth(2, int(widths[2]))
        else:
            self.table.setColumnWidth(0, 280)
            self.table.setColumnWidth(1, 100)
            self.table.setColumnWidth(2, 150)
        mode = self._settings.value(f"{self._key}/view_mode", "list")
        if mode == "grid":
            self._view_grid(remember=False)
        else:
            self._view_list(remember=False)

    def _save_widths(self):
        self._settings.setValue(
            f"{self._key}/col_widths",
            [
                self.table.columnWidth(0),
                self.table.columnWidth(1),
                self.table.columnWidth(2),
            ],
        )

    def _view_list(self, remember: bool = True):
        self.stack.setCurrentWidget(self.table)
        self.list_btn.setChecked(True)
        self.grid_btn.setChecked(False)
        if remember:
            self._settings.setValue(f"{self._key}/view_mode", "list")

    def _view_grid(self, remember: bool = True):
        self.stack.setCurrentWidget(self.grid)
        self.grid_btn.setChecked(True)
        self.list_btn.setChecked(False)
        if remember:
            self._settings.setValue(f"{self._key}/view_mode", "grid")

    # ---------- navigation ----------

    def navigate(self, path: str, record: bool = True):
        try:
            entries = self._list(path)
        except (AdbError, OSError) as e:
            self.status_message.emit(f"打开失败: {e}")
            return
        if record and self.current_path:
            self._back.append(self.current_path)
            self._fwd.clear()
        self.current_path = path
        self.model.removeRows(0, self.model.rowCount())
        for e in entries:
            if not self._show_hidden and e.name.startswith("."):
                continue
            self._add_row(e)
        self._sort_rows()
        self._update_path_combo()
        self._update_nav_btns()
        self.update_status()
        self._settings.setValue(f"{self._key}/last_path", path)
        self.path_changed.emit(path)

    def saved_path(self) -> str:
        return self._settings.value(f"{self._key}/last_path", "") or ""

    def go_back(self):
        if not self._back:
            return
        self._fwd.append(self.current_path)
        target = self._back.pop()
        self.navigate(target, record=False)

    def go_forward(self):
        if not self._fwd:
            return
        self._back.append(self.current_path)
        target = self._fwd.pop()
        self.navigate(target, record=False)

    def go_up(self):
        parent = str(Path(self.current_path).parent) if self.is_local else str(PurePosixPath(self.current_path).parent)
        if parent != self.current_path:
            self.navigate(parent)

    def _do_refresh(self):
        self.refresh_btn.setStyleSheet(self._refresh_active_style)
        self.refresh()

    def refresh(self):
        if self.current_path:
            self.navigate(self.current_path, record=False)
        self.refresh_btn.setStyleSheet(self._refresh_normal_style)

    def clear_content(self):
        self.current_path = ""
        self._back.clear()
        self._fwd.clear()
        self.model.removeRows(0, self.model.rowCount())
        if self.path_edit:
            self.path_edit.clear()
        elif self.path_combo:
            self.path_combo.setEditText("")
        self._update_nav_btns()
        self.update_status()

    def _jump_to_path(self, index=None):
        if self.path_edit:
            path = self.path_edit.text().strip()
        elif index is not None and isinstance(index, int):
            path = self.path_combo.itemText(index)
        else:
            path = self.path_combo.currentText().strip()
        if not path:
            return
        if self.is_local:
            if not os.path.isdir(path):
                self.status_message.emit(f"本地路径不存在: {path}")
                return
        elif not path.startswith("/"):
            self.status_message.emit("请输入绝对路径，如 /sdcard/Download")
            return
        self.navigate(path)

    def _update_path_combo(self):
        if self.path_edit:
            self.path_edit.setText(self.current_path)
            return
        self.path_combo.blockSignals(True)
        items = [self.path_combo.itemText(i) for i in range(self.path_combo.count())]
        if self.current_path not in items:
            self.path_combo.addItem(self.current_path)
        self.path_combo.setCurrentText(self.current_path)
        self.path_combo.blockSignals(False)

    def _browse_local_dir(self):
        start = self.path_edit.text() if self.path_edit else str(Path.home())
        import os as _os

        if not _os.path.isdir(start):
            start = str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "选择目录", start)
        if path:
            self.navigate(path)

    def _jump_to_edit(self):
        path = self.path_edit.text().strip()
        if not path:
            return
        import os as _os

        if not _os.path.isdir(path):
            self.status_message.emit(f"本地路径不存在: {path}")
            return
        self.navigate(path)

    def _seed_path_combo(self):
        if self.path_edit:
            return
        seeds: list[str]
        if self.is_local:
            home = Path.home()
            seeds = [
                str(home),
                str(home / "Desktop"),
                str(home / "Downloads"),
                str(home / "Documents"),
                str(home / "Pictures"),
                str(home / "Music"),
                str(home / "Movies"),
                "/",
            ]
            volumes = Path("/Volumes")
            if volumes.exists():
                seeds += [str(pp) for pp in sorted(volumes.iterdir()) if pp.is_dir()]
        else:
            seeds = [
                "/sdcard",
                "/sdcard/Download",
                "/sdcard/DCIM",
                "/sdcard/Documents",
                "/sdcard/Pictures",
                "/sdcard/Music",
                "/sdcard/Movies",
                "/storage/emulated/0",
                "/data",
                "/",
            ]
        existing = {self.path_combo.itemText(i) for i in range(self.path_combo.count())}
        for s in seeds:
            if s and s not in existing:
                self.path_combo.addItem(s)
                existing.add(s)

    def _update_nav_btns(self):
        self.back_btn.setEnabled(bool(self._back))
        self.fwd_btn.setEnabled(bool(self._fwd))
        self.up_btn.setEnabled(self.current_path not in ("/", str(Path.home()).rstrip("/")))

    def _toggle_hidden(self, on: bool):
        self._show_hidden = on
        self.refresh()

    # ---------- sorting ----------

    def _on_header_clicked(self, col: int):
        if self._sort_col == col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            self._sort_asc = True
        self._sort_rows()

    def _sort_rows(self):
        rows = [self.model.takeRow(0) for _ in range(self.model.rowCount())]
        rows.sort(
            key=lambda items: items[self._sort_col].data(ROLE_SORT),
            reverse=not self._sort_asc,
        )
        for items in rows:
            self.model.appendRow(items)

    # ---------- listing ----------

    def _list(self, path: str) -> list[FileEntry]:
        return self._list_local(path) if self.is_local else self.adb.list_dir(path)

    def _list_local(self, path: str) -> list[FileEntry]:
        entries: list[FileEntry] = []
        try:
            names = sorted(os.listdir(path), key=str.lower)
        except OSError as e:
            raise AdbError(str(e))
        for name in names:
            full = os.path.join(path, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            entries.append(
                FileEntry(
                    name=name,
                    path=full,
                    is_dir=os.path.isdir(full),
                    size=st.st_size if not os.path.isdir(full) else 0,
                    mtime=datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                )
            )
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    def _add_row(self, e: FileEntry):
        name_item = QStandardItem(e.name)
        name_item.setData(e.path, ROLE_PATH)
        name_item.setData(e.is_dir, ROLE_IS_DIR)
        name_item.setData((0 if e.is_dir else 1, e.name.lower()), ROLE_SORT)
        name_item.setIcon(
            self._icons.icon(QFileIconProvider.IconType.Folder if e.is_dir else QFileIconProvider.IconType.File)
        )
        size_item = QStandardItem("" if e.is_dir else self._fmt_size(e.size))
        size_item.setData(e.size, ROLE_SORT)
        size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        time_item = QStandardItem(e.mtime)
        time_item.setData(e.mtime, ROLE_SORT)
        self.model.appendRow([name_item, size_item, time_item])

    @staticmethod
    def _fmt_size(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024 or unit == "TB":
                return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
            n /= 1024
        return f"{n} B"

    def update_status(self):
        rows = self.model.rowCount()
        sel = self.get_selected_paths()
        if sel:
            total = sum(self.model.item(r, 1).data(ROLE_SORT) for r in self._all_selected_rows())
            self.info_label.setText(f"{rows} 项 · 已选 {len(sel)} 项 · 合计 {self._fmt_size(total)}")
        else:
            self.info_label.setText(f"{rows} 项")

    # ---------- selection helpers ----------

    def _current_view(self):
        return self.stack.currentWidget()

    def get_cwd(self) -> str:
        return self.current_path

    def get_selected_paths(self) -> list[str]:
        rows = self._all_selected_rows()
        paths = []
        for r in rows:
            item = self.model.item(r, 0)
            if item:
                paths.append(item.data(ROLE_PATH))
        return paths

    def _all_selected_rows(self) -> list[int]:
        rows: set[int] = set()
        for view in (self.table, self.grid):
            rows |= {i.row() for i in view.selectionModel().selectedIndexes()}
        return sorted(rows)

    def _selected_entry(self) -> FileEntry | None:
        paths = self.get_selected_paths()
        if not paths:
            return None
        path = paths[0]
        for r in range(self.model.rowCount()):
            item = self.model.item(r, 0)
            if item and item.data(ROLE_PATH) == path:
                return FileEntry(
                    name=item.text(),
                    path=path,
                    is_dir=item.data(ROLE_IS_DIR),
                )
        return None

    def _on_double_click(self, index):
        self._enter()

    def _enter(self):
        e = self._selected_entry()
        if not e:
            return
        if e.is_dir:
            self.navigate(e.path)
        elif self.is_local:
            QDesktopServices.openUrl(QUrl.fromLocalFile(e.path))
        else:
            self.adb._run(
                [
                    "shell",
                    f"am start -a android.intent.action.VIEW -d file://{shlex.quote(e.path)}",
                ],
                check=False,
            )
            self.status_message.emit(f"已在手机上打开 {e.name}")

    # ---------- clipboard & ops ----------

    def copy_selection(self):
        paths = self.get_selected_paths()
        if not paths:
            return
        FilePanel._clipboard = (self.is_local, paths, False)
        self.status_message.emit(f"已复制 {len(paths)} 项")

    def cut_selection(self):
        paths = self.get_selected_paths()
        if not paths:
            return
        FilePanel._clipboard = (self.is_local, paths, True)
        self.status_message.emit(f"已剪切 {len(paths)} 项")

    def paste(self):
        if not FilePanel._clipboard:
            self.status_message.emit("剪贴板为空，请先复制或剪切")
            return
        src_local, paths, cut = FilePanel._clipboard
        if src_local == self.is_local:
            for p in paths:
                self._move_local(p, self.current_path, cut)
            if cut:
                FilePanel._clipboard = None
            self.refresh()
        else:
            if src_local:
                self.transfer_requested.emit(paths, self.current_path)
            else:
                self.download_requested.emit(paths, self.current_path)

    def _move_local(self, src: str, dest: str, cut: bool):
        import shutil

        try:
            if cut:
                shutil.move(src, dest)
            else:
                target = os.path.join(dest, os.path.basename(src))
                if os.path.isdir(src):
                    shutil.copytree(src, target)
                else:
                    shutil.copy2(src, target)
        except (OSError, shutil.Error) as e:
            self.status_message.emit(f"操作失败: {e}")

    def delete(self):
        paths = self.get_selected_paths()
        if not paths:
            return
        reply = QMessageBox.question(self, "确认删除", f"确定删除 {len(paths)} 项？此操作不可恢复。")
        if reply != QMessageBox.StandardButton.Yes:
            return
        for p in paths:
            try:
                if self.is_local:
                    if os.path.isdir(p):
                        import shutil

                        shutil.rmtree(p)
                    else:
                        os.remove(p)
                else:
                    self.adb.remove(p, is_dir=self._is_remote_dir(p))
            except (AdbError, OSError) as e:
                self.status_message.emit(f"删除失败: {e}")
        self.refresh()

    def _is_remote_dir(self, path: str) -> bool:
        for r in range(self.model.rowCount()):
            item = self.model.item(r, 0)
            if item and item.data(ROLE_PATH) == path:
                return bool(item.data(ROLE_IS_DIR))
        return False

    def mkdir(self):
        name, ok = QInputDialog.getText(self, "新建文件夹", "文件夹名称:")
        if not ok or not name.strip():
            return
        name = name.strip()
        try:
            if self.is_local:
                os.mkdir(os.path.join(self.current_path, name))
            else:
                self.adb.mkdir(f"{self.current_path}/{name}")
            self.refresh()
        except (AdbError, OSError) as e:
            QMessageBox.warning(self, "错误", str(e))

    def rename(self):
        e = self._selected_entry()
        if not e:
            return
        new_name, ok = QInputDialog.getText(self, "重命名", "新名称:", text=e.name)
        if not ok or not new_name.strip() or new_name.strip() == e.name:
            return
        try:
            if self.is_local:
                os.rename(e.path, os.path.join(self.current_path, new_name.strip()))
            else:
                self.adb.move(e.path, f"{self.current_path}/{new_name.strip()}")
            self.refresh()
        except (AdbError, OSError) as ex:
            QMessageBox.warning(self, "错误", str(ex))

    # ---------- drag & drop ----------

    def _on_drop(self, src_is_local: bool, paths: list[str], dest: str | None = None):
        import shutil

        if not paths:
            return
        dest = dest or self.current_path
        if src_is_local == self.is_local:
            # same side: move into this panel's directory
            moved = 0
            for p in paths:
                try:
                    if self.is_local:
                        if Path(p).parent.resolve() != Path(dest).resolve():
                            shutil.move(p, dest)
                            moved += 1
                    else:
                        self.adb.move(p, dest)
                        moved += 1
                except (OSError, AdbError, shutil.Error) as e:
                    self.status_message.emit(f"移动失败: {e}")
            if moved:
                self.status_message.emit(f"已移动 {moved} 项")
            self.refresh()
        elif src_is_local:
            # local -> phone: transfer into phone current dir
            self.transfer_requested.emit(paths, dest)
        else:
            # phone -> local: transfer into local current dir
            self.download_requested.emit(paths, dest)

    # ---------- transfer entry points ----------

    def _to_phone(self):
        if self.is_local:
            self.transfer_requested.emit(self.get_selected_paths(), self.current_path)

    def _to_computer(self):
        if not self.is_local:
            self.download_requested.emit(self.get_selected_paths(), self.current_path)

    def _install_apks(self):
        if not self.is_local:
            return
        apks = [p for p in self.get_selected_paths() if p.lower().endswith(".apk")]
        if not apks:
            self.status_message.emit("请先选中 .apk 文件")
            return
        self.install_requested.emit(apks)

    # ---------- context menu ----------

    def _show_context_menu(self, pos):
        menu = QMenu(self)
        sel = self.get_selected_paths()
        if sel:
            if self.is_local:
                menu.addAction("传输到手机当前目录", self._to_phone)
                if any(p.lower().endswith(".apk") for p in sel):
                    menu.addAction("安装到手机", self._install_apks)
            else:
                menu.addAction("传输到电脑当前目录", self._to_computer)
                menu.addAction("在手机上打开", self._open_on_device)
            menu.addSeparator()
            menu.addAction("剪切", self.cut_selection)
            menu.addAction("复制", self.copy_selection)
            menu.addSeparator()
            menu.addAction("重命名", self.rename)
            menu.addAction("删除", self.delete)
        else:
            menu.addAction("粘贴", self.paste)
        menu.addSeparator()
        menu.addAction("新建文件夹", self.mkdir)
        menu.addAction("刷新", self.refresh)
        menu.exec(self._current_view().viewport().mapToGlobal(pos))

    def _open_on_device(self):
        paths = self.get_selected_paths()
        if not paths:
            return
        self.adb._run(
            [
                "shell",
                f"am start -a android.intent.action.VIEW -d file://{shlex.quote(paths[0])}",
            ],
            check=False,
        )
        self.status_message.emit("已在手机上打开")
