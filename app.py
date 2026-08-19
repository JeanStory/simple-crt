"""程序入口。

无主密码: 密码加密的数据密钥由 Windows DPAPI 绑定当前用户自动管理
(见 crypto.py / sessions.py), 启动全程透明, 无需任何输入。
"""
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from .mainwindow import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Simple CRT")
    win = MainWindow()  # SessionStore 构造时自动加载/生成 DPAPI 密钥
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
