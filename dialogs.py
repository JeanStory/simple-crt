"""连接编辑对话框。"""
from __future__ import annotations

from PySide6.QtWidgets import (QDialog, QVBoxLayout, QFormLayout, QLineEdit,
                               QComboBox, QSpinBox, QDialogButtonBox, QStackedWidget,
                               QWidget, QPushButton, QHBoxLayout, QFileDialog)

from .sessions import Session


class SessionDialog(QDialog):
    """新建/编辑会话。"""

    def __init__(self, parent=None, session: Session | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("会话配置")
        self.setMinimumWidth(420)
        self._build()
        if session:
            self._load(session)
        self._on_kind_changed()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.name_edit = QLineEdit()
        self.kind_combo = QComboBox()
        self.kind_combo.addItems(["ssh", "serial", "local"])
        self.kind_combo.currentTextChanged.connect(self._on_kind_changed)
        form.addRow("名称", self.name_edit)
        form.addRow("类型", self.kind_combo)
        layout.addLayout(form)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_ssh())
        self.stack.addWidget(self._build_serial())
        self.stack.addWidget(self._build_local())
        layout.addWidget(self.stack)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _build_ssh(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.host_edit = QLineEdit()
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(22)
        self.user_edit = QLineEdit()
        self.pass_edit = QLineEdit()
        self.pass_edit.setEchoMode(QLineEdit.Password)
        key_row = QHBoxLayout()
        self.key_edit = QLineEdit()
        key_btn = QPushButton("浏览")
        key_btn.clicked.connect(self._pick_key)
        key_row.addWidget(self.key_edit)
        key_row.addWidget(key_btn)
        form.addRow("主机", self.host_edit)
        form.addRow("端口", self.port_spin)
        form.addRow("用户名", self.user_edit)
        form.addRow("密码", self.pass_edit)
        form.addRow("私钥", key_row)
        return w

    def _build_serial(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.serial_port_edit = QLineEdit()
        self.serial_port_edit.setPlaceholderText("COM3 或 /dev/ttyUSB0")
        self.baud_combo = QComboBox()
        self.baud_combo.setEditable(True)
        self.baud_combo.addItems(["9600", "19200", "38400", "57600",
                                  "115200", "230400", "460800", "921600"])
        self.baud_combo.setCurrentText("115200")
        form.addRow("串口", self.serial_port_edit)
        form.addRow("波特率", self.baud_combo)
        return w

    def _build_local(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.shell_edit = QLineEdit()
        self.shell_edit.setPlaceholderText("留空使用系统默认 shell")
        form.addRow("Shell", self.shell_edit)
        return w

    def _pick_key(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择私钥文件")
        if path:
            self.key_edit.setText(path)

    def _on_kind_changed(self, *_) -> None:
        self.stack.setCurrentIndex(self.kind_combo.currentIndex())

    def _load(self, s: Session) -> None:
        self.name_edit.setText(s.name)
        self.kind_combo.setCurrentText(s.kind)
        self.host_edit.setText(s.host)
        self.port_spin.setValue(s.port or 22)
        self.user_edit.setText(s.username)
        self.pass_edit.setText(s.password)
        self.key_edit.setText(s.key_path)
        self.serial_port_edit.setText(s.serial_port)
        self.baud_combo.setCurrentText(str(s.baudrate))
        self.shell_edit.setText(s.shell)

    def get_session(self) -> Session:
        kind = self.kind_combo.currentText()
        name = self.name_edit.text().strip()
        if not name:
            if kind == "ssh":
                name = self.host_edit.text().strip() or "ssh"
            elif kind == "serial":
                name = self.serial_port_edit.text().strip() or "serial"
            else:
                name = "local"
        try:
            baud = int(self.baud_combo.currentText())
        except ValueError:
            baud = 115200
        return Session(
            name=name,
            kind=kind,
            host=self.host_edit.text().strip(),
            port=self.port_spin.value(),
            username=self.user_edit.text().strip(),
            password=self.pass_edit.text(),
            key_path=self.key_edit.text().strip(),
            serial_port=self.serial_port_edit.text().strip(),
            baudrate=baud,
            shell=self.shell_edit.text().strip(),
        )
