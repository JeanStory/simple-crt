"""ZMODEM UI 控制器: 在终端数据流中拦截 rz/sz 启动序列, 驱动文件传输。

挂载点: TerminalTab._on_data 把远端字节先交给 ZmodemController.feed()。
- 非传输态: 探测 ZMODEM 启动头; 未命中的字节照常送终端渲染。
- 命中下载(sz): 弹目录选择框 -> ZModemReceiver 接收文件到本地。
- 命中上传(rz): 弹文件选择框 -> ZModemSender 发送本地文件到服务器。
- 传输态: 所有远端字节导流给会话状态机, 不再进终端渲染; finished 后退出拦截态。

设计取舍(首版):
- 探测缓冲保留尾部 KEEP 字节, 防启动头被 TCP 分块切断而漏检。
- 进度用非模态 QProgressDialog; 取消调用 session.abort() (若可用)。
- feed() 在 UI 线程执行(经 _DataBridge 信号), 弹窗期间新数据由 Qt 事件队列缓冲, 可接受。
"""
from __future__ import annotations

import os

from PySide6.QtWidgets import QFileDialog, QProgressDialog, QMessageBox
from PySide6.QtCore import Qt

from .zmodem import (
    ZModemReceiver, ZModemSender, detect_zmodem, cancel_sequence, ZDLE, ZPAD)


# 启动头最长约 20 字节(hex header); 保留 32 字节足以跨块拼回启动序列。
_KEEP = 32


class ZmodemController:
    def __init__(self, tab) -> None:
        self.tab = tab
        self.session = None          # None | ZModemReceiver | ZModemSender
        self._scan = bytearray()     # 非传输态探测缓冲
        self._progress = None
        self._save_dir = None
        self._cancelled = False

    # ---- 对外: 远端字节入口 ----
    def feed(self, data: bytes) -> None:
        # 传输态: 全部导流给会话
        if self.session is not None:
            try:
                self.session.feed(bytes(data))
            except Exception as e:  # 会话内部已自我防御, 这里兜底
                self._term(("\r\n\x1b[31m*** ZMODEM 会话异常: %s ***\x1b[0m\r\n" % e).encode())
                self._end_session()
                return
            if getattr(self.session, "finished", False):
                self._end_session()
            return

        # 非传输态: 探测启动序列
        self._scan += data
        res = detect_zmodem(bytes(self._scan))
        if res is not None:
            mode, idx = res
            pre = bytes(self._scan[:idx])
            if pre:
                self._term(pre)            # 序列之前的字节是正常终端输出
            rest = bytes(self._scan[idx:])
            self._scan.clear()
            self._start(mode, rest)
            return

        # 未命中: 立即 flush 给终端, 仅保留尾部"可能是触发序列被分包切断的前缀"。
        # 触发序列判定为 \x18B0 / \x18B1 (可选前置 ZPAD '*')。真前缀只有 \x18 与 \x18B,
        # 其中 \x18(CAN) 是控制符, 正常终端输出几乎不出现 —— 因此普通交互字节全部
        # 立即回显, 不再被无脑扣留。旧实现无条件保留尾部 _KEEP 字节, 会吞掉交互
        # 回显(每次仅几字节 < _KEEP), 造成"输入不显示、回车后才一次性刷出"。
        keep = self._keep_from()
        if keep > 0:
            flush = bytes(self._scan[:keep])
            self._term(flush)
            del self._scan[:keep]

    def _keep_from(self) -> int:
        """返回需保留的尾部起始下标: 尾部若为触发序列前缀 ``(ZPAD*) ZDLE (B)?``
        则从该 ZDLE(含其前置连续 ZPAD)起保留, 等后续字节到齐再判; 否则返回
        len(scan) 表示全部可 flush。"""
        scan = self._scan
        n = len(scan)
        k = n
        if k > 0 and scan[k - 1] == ord("B"):
            # 尾部 'B': 仅当其前为 ZDLE 才是前缀 \x18B, 否则是普通字符
            if k >= 2 and scan[k - 2] == ZDLE:
                k -= 2
            else:
                return n
        elif k > 0 and scan[k - 1] == ZDLE:
            k -= 1
        else:
            return n
        # k 指向 ZDLE, 回退前置连续 ZPAD
        while k > 0 and scan[k - 1] == ZPAD:
            k -= 1
        return k

    # ---- 内部 ----
    def _term(self, data: bytes) -> None:
        self.tab.term.feed(data)

    def _write(self, data: bytes) -> None:
        conn = self.tab.connection
        if conn and conn.alive:
            conn.write(data)

    def _cancel_remote(self) -> None:
        """用户放弃传输: 向远端发 ZMODEM 取消序列(8xCAN+8x退格)中止 sz/rz 进程,
        并清空探测缓冲, 防残留协议字节再次误触发拦截。"""
        self._write(cancel_sequence())
        self._scan.clear()
        self.session = None

    def _start(self, mode: str, rest: bytes) -> None:
        if mode == "download":
            self._start_download(rest)
        elif mode == "upload":
            self._start_upload(rest)

    # ---- 下载(服务器 sz -> 本地接收) ----
    def _start_download(self, rest: bytes) -> None:
        save_dir = QFileDialog.getExistingDirectory(
            self.tab, "选择接收文件的保存目录", os.path.expanduser("~"))
        if not save_dir:
            # 用户取消: 发 ZMODEM 取消序列(8xCAN+8x退格)中止远端 sz 进程,
            # 否则远端持续重发 ZRQINIT 头, 每次都再触发目录选择框(死循环)。
            # rest 是协议启动头(\x18B00...), 不透传给终端(否则显示乱码)。
            self._cancel_remote()
            self._term("\r\n\x1b[33m*** 已取消接收 ***\x1b[0m\r\n".encode())
            return
        self._save_dir = save_dir
        self._cancelled = False

        recv = ZModemReceiver(
            write_fn=self._write,
            on_file_meta=self._on_recv_meta,
            on_progress=self._on_progress,
            on_done=self._on_recv_file_done,
            on_finish=self._on_finish,
            on_error=self._on_error,
        )
        self.session = recv
        recv.start()
        if rest:
            recv.feed(rest)
        if getattr(recv, "finished", False):
            self._end_session()

    def _on_recv_meta(self, name: str, size: int):
        # 只取 basename, 防路径穿越
        base = os.path.basename(name.replace("\\", "/")) or "received.bin"
        path = os.path.join(self._save_dir, base)
        # 目标已存在 -> 询问是否覆盖; 拒绝则返回 None 让接收器发 ZSKIP 跳过该文件
        if os.path.exists(path):
            resp = QMessageBox.question(
                self.tab,
                "文件已存在",
                "本地已存在同名文件:\n%s\n\n是否覆盖?" % path,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if resp != QMessageBox.Yes:
                self._term(("\r\n\x1b[33m*** 已跳过(文件已存在): %s ***\x1b[0m\r\n" % base).encode())
                return None
        self._ensure_progress("接收: %s" % base, size)
        return path

    def _on_recv_file_done(self, name: str, path: str) -> None:
        self._term(("\r\n\x1b[32m*** 已接收: %s ***\x1b[0m\r\n" % path).encode())

    # ---- 上传(服务器 rz -> 本地发送) ----
    def _start_upload(self, rest: bytes) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self.tab, "选择要上传的文件", os.path.expanduser("~"))
        if not paths:
            # 用户取消: 发取消序列中止远端 rz 进程, 不透传协议头。
            self._cancel_remote()
            self._term("\r\n\x1b[33m*** 已取消上传 ***\x1b[0m\r\n".encode())
            return
        self._cancelled = False

        def _make_opener(p):
            return lambda: open(p, "rb")

        files = []
        for p in paths:
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            name = os.path.basename(p.replace("\\", "/")) or "upload.bin"
            files.append((p, name, size, _make_opener(p)))

        sender = ZModemSender(
            files=files,
            write_fn=self._write,
            on_progress=self._on_progress,
            on_done=self._on_send_file_done,
            on_finish=self._on_finish,
            on_error=self._on_error,
            on_skip=self._on_send_file_skip,
        )
        self.session = sender
        total = 0
        try:
            total = sum(os.path.getsize(p) for p in paths)
        except OSError:
            total = 0
        self._ensure_progress("上传 %d 个文件" % len(paths), total)
        sender.start()
        if rest:
            sender.feed(rest)
        if getattr(sender, "finished", False):
            self._end_session()

    def _on_send_file_done(self, name: str, path: str) -> None:
        self._term(("\r\n\x1b[32m*** 已上传: %s ***\x1b[0m\r\n" % os.path.basename(path)).encode())

    def _on_send_file_skip(self, name: str, path: str) -> None:
        # 远端已存在同名文件, 服务器拒收 -> 提示跳过
        label = os.path.basename(path) if path else (name or "")
        self._term(("\r\n\x1b[33m*** 已跳过(远端文件已存在): %s ***\x1b[0m\r\n" % label).encode())

    # ---- 进度 ----
    def _ensure_progress(self, label: str, total: int) -> None:
        if self._progress is None:
            dlg = QProgressDialog(label, "取消", 0, 100, self.tab)
            dlg.setWindowTitle("ZMODEM 文件传输")
            dlg.setWindowModality(Qt.NonModal)
            dlg.setMinimumDuration(0)
            dlg.setAutoClose(False)
            dlg.setAutoReset(False)
            dlg.canceled.connect(self._on_cancel)
            self._progress = dlg
        self._progress.setLabelText(label)
        self._progress.setValue(0)
        self._progress.show()

    def _on_progress(self, name: str, done: int, size: int) -> None:
        if self._progress is None:
            return
        pct = int(done * 100 / size) if size > 0 else 0
        self._progress.setLabelText("%s  (%d / %d 字节)" % (os.path.basename(name), done, size))
        self._progress.setValue(min(pct, 100))

    def _on_cancel(self) -> None:
        self._cancelled = True
        abort = getattr(self.session, "abort", None)
        if callable(abort):
            try:
                abort()
            except Exception:
                pass
        self._term("\r\n\x1b[33m*** 传输已取消 ***\x1b[0m\r\n".encode())
        self._end_session()

    # ---- 完成/错误 ----
    def _on_finish(self) -> None:
        # 整个会话所有文件完成
        pass

    def _on_error(self, msg: str) -> None:
        self._term(("\r\n\x1b[31m*** ZMODEM 错误: %s ***\x1b[0m\r\n" % msg).encode())
        self._end_session()

    def _end_session(self) -> None:
        self.session = None
        self._scan.clear()
        if self._progress is not None:
            self._progress.reset()
            self._progress.hide()
