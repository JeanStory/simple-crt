"""Simple CRT 启动入口。

目录名 "simple-crt" 含连字符, 不是合法的 Python 包名, 无法用
`python -m simple-crt.app` 直接运行。这里把该目录以合法包名 "simplecrt"
动态注册, 使包内的相对导入 (from .mainwindow import ...) 正常解析。

同时兼容两种运行态:
  - 开发态:  python run.py        (源码就在本目录)
  - 冻结态:  PyInstaller 打的 exe (源码收集到 sys._MEIPASS/app_src)
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys

# 显式 import 第三方依赖, 触发 PyInstaller 静态分析收集
# (真正用到它们的是下面动态加载的子模块, PyInstaller 扫不到, 故在此点名)
import PySide6.QtCore      # noqa: F401
import PySide6.QtGui       # noqa: F401
import PySide6.QtWidgets   # noqa: F401
import pyte                # noqa: F401
import cryptography        # noqa: F401


def _source_dir() -> str:
    """返回包源码所在目录 (含 __init__.py)。"""
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "app_src")  # type: ignore[attr-defined]
    return os.path.dirname(os.path.abspath(__file__))


def _register_package(pkg_name: str, pkg_dir: str) -> None:
    """把 pkg_dir 以合法包名 pkg_name 注册到 sys.modules。"""
    init_py = os.path.join(pkg_dir, "__init__.py")
    spec = importlib.util.spec_from_file_location(
        pkg_name, init_py, submodule_search_locations=[pkg_dir]
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载包 {pkg_name} (目录: {pkg_dir})")
    module = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = module
    spec.loader.exec_module(module)


def main() -> int:
    _register_package("simplecrt", _source_dir())
    app = importlib.import_module("simplecrt.app")
    return app.main()


if __name__ == "__main__":
    sys.exit(main())
