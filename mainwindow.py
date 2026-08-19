"""主窗口: 会话树 + 标签页终端。"""
from __future__ import annotations

from PySide6.QtCore import Qt, QObject, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QTabWidget,
                               QDockWidget, QTreeWidget, QTreeWidgetItem, QMenu,
                               QMessageBox, QInputDialog, QToolBar, QLabel)

from .sessions import SessionStore, Session
from .dialogs import SessionDialog
from .terminal import TerminalWidget
from .connections import SSHConnection, SerialConnection, LocalConnection


class _DataBridge(QObject):
    """把后台线程回调转成 Qt 信号, 保证在 UI 线程执行。"""
    data_ready = Signal(bytes)
    closed = Signal(str)
    connected = Signal(str)         # 连接成功 (会话名)
    connect_failed = Signal(str)    # 连接失败 (错误信息)


class TerminalTab(QWidget):
    """一个终端标签: 终端控件 + 连接后端。"""

    def __init__(self, session: Session, parent=None) -> None:
        super().__init__(parent)
        self.session = session
        self.connection = None
        self.bridge = _DataBridge()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.term = TerminalWidget()
        layout.addWidget(self.term)

        self.term.send_data.connect(self._on_send)
        self.term.resized.connect(self._on_resize)
        self.bridge.data_ready.connect(self._on_data)
        self.bridge.closed.connect(self._on_closed)
        self.bridge.connected.connect(self._on_connected)
        self.bridge.connect_failed.connect(self._on_connect_failed)

    def connect_backend(self) -> None:
        import threading
        s = self.session
        if s.kind == "ssh":
            conn = SSHConnection(s.host, s.port, s.username, s.password, s.key_path)
        elif s.kind == "serial":
            conn = SerialConnection(s.serial_port, s.baudrate)
        else:
            conn = LocalConnection(s.shell)
        conn.on_data = lambda d: self.bridge.data_ready.emit(d)
        conn.on_close = lambda r: self.bridge.closed.emit(r)
        self.connection = conn

        # 提示正在连接, 并把可能阻塞的 start() 放到后台线程, 避免冻结 UI
        self.term.feed(("\x1b[33m*** 正在连接 %s ...\x1b[0m\r\n" % s.name).encode())
        cols, rows = self.term.cols, self.term.rows

        def _worker():
            try:
                conn.start(cols, rows)
                self.bridge.connected.emit(s.name)
            except Exception as e:
                self.bridge.connect_failed.emit(str(e))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_connected(self, name: str) -> None:
        self.term.feed(("\x1b[32m*** 已连接: %s ***\x1b[0m\r\n" % name).encode())

    def _on_connect_failed(self, err: str) -> None:
        self.term.feed(("\x1b[31m*** 连接失败: %s ***\x1b[0m\r\n" % err).encode())

    def _on_send(self, data: bytes) -> None:
        if self.connection and self.connection.alive:
            self.connection.write(data)

    def _on_resize(self, cols: int, rows: int) -> None:
        if self.connection and self.connection.alive:
            self.connection.resize(cols, rows)

    def _on_data(self, data: bytes) -> None:
        self.term.feed(data)

    def _on_closed(self, reason: str) -> None:
        self.term.feed(("\r\n\x1b[33m*** 连接已断开: %s ***\x1b[0m\r\n" % reason).encode())

    def close_backend(self) -> None:
        if self.connection:
            self.connection.close()

    @property
    def is_alive(self) -> bool:
        return bool(self.connection and self.connection.alive)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Simple CRT - 简易终端工具")
        self.resize(1000, 680)
        self.store = SessionStore()

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.setCentralWidget(self.tabs)

        self._build_session_dock()
        self._build_toolbar()
        self._build_menu()
        self.statusBar().showMessage("就绪")

    # ---------- 会话侧栏 ----------
    def _build_session_dock(self) -> None:
        self.dock = QDockWidget("会话", self)
        self.dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemDoubleClicked.connect(self._on_tree_double)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.dock.setWidget(self.tree)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.dock)
        self._refresh_tree()

    def _refresh_tree(self) -> None:
        self.tree.clear()
        for i, s in enumerate(self.store.sessions):
            item = QTreeWidgetItem([f"{s.name}  ({s.kind})"])
            item.setData(0, Qt.UserRole, i)
            self.tree.addTopLevelItem(item)

    def _on_tree_double(self, item: QTreeWidgetItem) -> None:
        idx = item.data(0, Qt.UserRole)
        if idx is not None:
            self._open_session(self.store.sessions[idx])

    def _tree_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        menu = QMenu(self)
        if item is not None:
            idx = item.data(0, Qt.UserRole)
            menu.addAction("连接", lambda: self._open_session(self.store.sessions[idx]))
            menu.addAction("编辑", lambda: self._edit_session(idx))
            menu.addAction("删除", lambda: self._delete_session(idx))
        else:
            menu.addAction("新建会话", self._new_session)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    # ---------- 会话增删改 ----------
    def _new_session(self) -> None:
        dlg = SessionDialog(self)
        if dlg.exec():
            self.store.add(dlg.get_session())
            self._refresh_tree()

    def _edit_session(self, idx: int) -> None:
        dlg = SessionDialog(self, self.store.sessions[idx])
        if dlg.exec():
            self.store.update(idx, dlg.get_session())
            self._refresh_tree()

    def _delete_session(self, idx: int) -> None:
        s = self.store.sessions[idx]
        if QMessageBox.question(self, "删除", f"确定删除会话 '{s.name}'?") == QMessageBox.Yes:
            self.store.remove(idx)
            self._refresh_tree()

    # ---------- 打开连接 ----------
    def _open_session(self, session: Session) -> None:
        tab = TerminalTab(session)
        idx = self.tabs.addTab(tab, session.name)
        self.tabs.setCurrentIndex(idx)
        tab.connect_backend()
        tab.term.setFocus()

    def _quick_connect(self) -> None:
        dlg = SessionDialog(self)
        if dlg.exec():
            s = dlg.get_session()
            self._open_session(s)

    def _close_tab(self, index: int) -> None:
        tab = self.tabs.widget(index)
        if isinstance(tab, TerminalTab):
            tab.close_backend()
        self.tabs.removeTab(index)

    # ---------- 工具栏/菜单 ----------
    def _build_toolbar(self) -> None:
        tb = QToolBar("主工具栏")
        self.addToolBar(tb)
        tb.addAction(QAction("快速连接", self, triggered=self._quick_connect))
        tb.addAction(QAction("新建会话", self, triggered=self._new_session))
        tb.addAction(QAction("断开", self, triggered=self._disconnect_current))
        tb.addAction(QAction("清屏", self, triggered=self._clear_current))

    def _build_menu(self) -> None:
        m_file = self.menuBar().addMenu("文件")
        act_quick = QAction("快速连接", self, triggered=self._quick_connect)
        act_quick.setShortcut(QKeySequence("Ctrl+N"))
        m_file.addAction(act_quick)
        m_file.addAction(QAction("新建会话", self, triggered=self._new_session))
        m_file.addSeparator()
        m_file.addAction(QAction("退出", self, triggered=self.close))

        m_edit = self.menuBar().addMenu("编辑")
        m_edit.addAction(QAction("清屏", self, triggered=self._clear_current))
        m_edit.addAction(QAction("断开当前", self, triggered=self._disconnect_current))

        m_view = self.menuBar().addMenu("视图")
        m_view.addAction(self.dock.toggleViewAction())

    def _current_tab(self) -> TerminalTab | None:
        w = self.tabs.currentWidget()
        return w if isinstance(w, TerminalTab) else None

    def _disconnect_current(self) -> None:
        tab = self._current_tab()
        if tab:
            tab.close_backend()

    def _clear_current(self) -> None:
        tab = self._current_tab()
        if tab:
            tab.term.clear_scrollback()
            tab.term.feed(b"\x1b[2J\x1b[H")

    def closeEvent(self, event) -> None:
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, TerminalTab):
                w.close_backend()
        event.accept()
