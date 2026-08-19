"""终端控件: pyte 屏幕缓冲 + QPainter 渲染 + 键盘输入映射为 ANSI 序列。"""
from __future__ import annotations

import pyte
from PySide6.QtCore import Qt, QTimer, Signal, QRect
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QPainter, QKeyEvent,
                           QTextOption, QGuiApplication)
from PySide6.QtWidgets import QWidget, QAbstractScrollArea

# xterm 256 -> RGB 基础 16 色 + 命名色
NAMED_COLORS = {
    "black": "#2e3436", "red": "#cc0000", "green": "#4e9a06",
    "brown": "#c4a000", "yellow": "#c4a000", "blue": "#3465a4",
    "magenta": "#75507b", "cyan": "#06989a", "white": "#d3d7cf",
    "brightblack": "#555753", "brightred": "#ef2929",
    "brightgreen": "#8ae234", "brightyellow": "#fce94f",
    "brightblue": "#729fcf", "brightmagenta": "#ad7fa8",
    "brightcyan": "#34e2e2", "brightwhite": "#eeeeec",
}

DEFAULT_FG = "#d3d7cf"
DEFAULT_BG = "#1e1e1e"


def _xterm256_to_hex(n: int) -> str:
    if n < 16:
        base = [
            "000000", "cc0000", "4e9a06", "c4a000", "3465a4", "75507b",
            "06989a", "d3d7cf", "555753", "ef2929", "8ae234", "fce94f",
            "729fcf", "ad7fa8", "34e2e2", "eeeeec",
        ]
        return "#" + base[n]
    if n < 232:
        n -= 16
        r = n // 36
        g = (n % 36) // 6
        b = n % 6
        levels = [0, 95, 135, 175, 215, 255]
        return "#%02x%02x%02x" % (levels[r], levels[g], levels[b])
    # 灰阶
    v = 8 + (n - 232) * 10
    return "#%02x%02x%02x" % (v, v, v)


def resolve_color(spec: str, default: str) -> QColor:
    """pyte 的颜色可能是命名色/6位hex/'default'。"""
    if not spec or spec == "default":
        return QColor(default)
    s = spec.lower()
    if s in NAMED_COLORS:
        return QColor(NAMED_COLORS[s])
    # pyte 存 6 位 hex 字符串(无 #)
    if len(s) == 6:
        try:
            return QColor("#" + s)
        except Exception:
            pass
    # 纯数字 -> 256 色索引
    if s.isdigit():
        return QColor(_xterm256_to_hex(int(s)))
    return QColor(default)


class TerminalWidget(QAbstractScrollArea):
    """渲染 pyte 屏幕的终端控件。发送数据经 send_data 信号。"""

    send_data = Signal(bytes)
    resized = Signal(int, int)  # cols, rows

    def __init__(self, parent=None, font_family="Consolas", font_size=11,
                 history=5000) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)
        self.viewport().setAttribute(Qt.WA_OpaquePaintEvent, True)

        self._font = QFont(font_family, font_size)
        self._font.setStyleHint(QFont.Monospace)
        self._fm = QFontMetricsF(self._font)
        self._char_w = self._fm.horizontalAdvance("W")
        self._char_h = self._fm.height()

        self.cols = 80
        self.rows = 24
        self.screen = pyte.HistoryScreen(self.cols, self.rows, history=history, ratio=0.5)
        self.stream = pyte.ByteStream(self.screen)

        self._scroll_offset = 0  # 向上回滚的行数
        self._blink = True
        self._blink_timer = QTimer(self)
        self._blink_timer.timeout.connect(self._toggle_blink)
        self._blink_timer.start(500)

        self.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self.setStyleSheet("QAbstractScrollArea{background:%s;}" % DEFAULT_BG)

    # ---------- 数据流 ----------
    def feed(self, data: bytes) -> None:
        try:
            self.stream.feed(data)
        except Exception:
            pass
        # 有新输出时跳回底部
        self._scroll_offset = 0
        self._update_scrollbar()
        self.viewport().update()

    def _toggle_blink(self) -> None:
        self._blink = not self._blink
        self.viewport().update()

    # ---------- 尺寸 ----------
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._recalc_grid()

    def _recalc_grid(self) -> None:
        vp = self.viewport()
        new_cols = max(20, int(vp.width() / self._char_w))
        new_rows = max(5, int(vp.height() / self._char_h))
        if new_cols != self.cols or new_rows != self.rows:
            self.cols = new_cols
            self.rows = new_rows
            try:
                self.screen.resize(self.rows, self.cols)
            except Exception:
                pass
            self._update_scrollbar()
            self.resized.emit(self.cols, self.rows)
        self.viewport().update()

    def _update_scrollbar(self) -> None:
        hist = len(self.screen.history.top)
        bar = self.verticalScrollBar()
        bar.blockSignals(True)
        bar.setRange(0, hist)
        bar.setPageStep(self.rows)
        bar.setValue(hist - self._scroll_offset)
        bar.blockSignals(False)

    def _on_scroll(self, value: int) -> None:
        hist = len(self.screen.history.top)
        self._scroll_offset = max(0, hist - value)
        self.viewport().update()

    # ---------- 渲染 ----------
    def paintEvent(self, event) -> None:
        painter = QPainter(self.viewport())
        painter.fillRect(self.viewport().rect(), QColor(DEFAULT_BG))
        painter.setFont(self._font)

        buf = self.screen.buffer
        hist_top = list(self.screen.history.top)

        # 组合"历史 + 当前屏幕"的可视区
        for row in range(self.rows):
            src_index = row - self._scroll_offset
            if src_index < 0:
                # 显示历史行
                h_idx = len(hist_top) + src_index
                if 0 <= h_idx < len(hist_top):
                    line = hist_top[h_idx]
                    self._draw_line(painter, row, line, is_history=True)
            else:
                line = buf[src_index]
                self._draw_line(painter, row, line, is_history=False)

        # 光标 (仅在未回滚且闪烁开时)
        if self._scroll_offset == 0 and self._blink and self.hasFocus():
            cx = self.screen.cursor.x
            cy = self.screen.cursor.y
            if not self.screen.cursor.hidden:
                x = cx * self._char_w
                y = cy * self._char_h
                painter.fillRect(QRect(int(x), int(y),
                                       max(2, int(self._char_w)), int(self._char_h)),
                                 QColor(DEFAULT_FG))
                # 光标下字符反色
                try:
                    ch = self.screen.buffer[cy][cx]
                    if ch.data.strip():
                        painter.setPen(QColor(DEFAULT_BG))
                        painter.drawText(QRect(int(x), int(y), int(self._char_w) + 2,
                                               int(self._char_h)),
                                         Qt.AlignLeft | Qt.AlignVCenter, ch.data)
                except Exception:
                    pass

    def _draw_line(self, painter: QPainter, row: int, line, is_history: bool) -> None:
        y = row * self._char_h
        # line 是 dict-like: 列 -> Char
        max_col = self.cols
        for col in range(max_col):
            char = line[col]
            data = char.data or " "
            if data == " " and char.bg == "default" and not char.reverse:
                continue
            fg = resolve_color(char.fg, DEFAULT_FG)
            bg = resolve_color(char.bg, DEFAULT_BG)
            if char.reverse:
                fg, bg = bg, fg
            x = col * self._char_w
            rect = QRect(int(x), int(y), int(self._char_w) + 1, int(self._char_h) + 1)
            if bg != QColor(DEFAULT_BG):
                painter.fillRect(rect, bg)
            if data != " ":
                f = QFont(self._font)
                if char.bold:
                    f.setBold(True)
                if char.italics:
                    f.setItalic(True)
                if char.underscore:
                    f.setUnderline(True)
                painter.setFont(f)
                painter.setPen(fg)
                painter.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, data)

    # ---------- 键盘输入 ----------
    def event(self, event) -> bool:
        # Qt 默认会把 Tab/Backtab 当作焦点切换在 keyPressEvent 之前拦截,
        # 导致终端收不到 Tab(焦点跳走 => 看起来卡住)。这里拦下交给 keyPressEvent。
        from PySide6.QtCore import QEvent
        if event.type() == QEvent.KeyPress and event.key() in (Qt.Key_Tab, Qt.Key_Backtab):
            self.keyPressEvent(event)
            return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        mod = event.modifiers()
        text = event.text()

        seq = self._map_key(key, mod, text)
        if seq is not None:
            self._scroll_offset = 0
            self.send_data.emit(seq)
            event.accept()
        else:
            super().keyPressEvent(event)

    def _map_key(self, key, mod, text) -> bytes | None:
        ctrl = mod & Qt.ControlModifier
        # Ctrl+字母 -> 控制字符
        if ctrl and Qt.Key_A <= key <= Qt.Key_Z:
            return bytes([key - Qt.Key_A + 1])

        special = {
            Qt.Key_Return: b"\r", Qt.Key_Enter: b"\r",
            Qt.Key_Backspace: b"\x7f", Qt.Key_Tab: b"\t",
            Qt.Key_Backtab: b"\x1b[Z",
            Qt.Key_Escape: b"\x1b",
            Qt.Key_Up: b"\x1b[A", Qt.Key_Down: b"\x1b[B",
            Qt.Key_Right: b"\x1b[C", Qt.Key_Left: b"\x1b[D",
            Qt.Key_Home: b"\x1b[H", Qt.Key_End: b"\x1b[F",
            Qt.Key_PageUp: b"\x1b[5~", Qt.Key_PageDown: b"\x1b[6~",
            Qt.Key_Insert: b"\x1b[2~", Qt.Key_Delete: b"\x1b[3~",
            Qt.Key_F1: b"\x1bOP", Qt.Key_F2: b"\x1bOQ",
            Qt.Key_F3: b"\x1bOR", Qt.Key_F4: b"\x1bOS",
            Qt.Key_F5: b"\x1b[15~", Qt.Key_F6: b"\x1b[17~",
            Qt.Key_F7: b"\x1b[18~", Qt.Key_F8: b"\x1b[19~",
            Qt.Key_F9: b"\x1b[20~", Qt.Key_F10: b"\x1b[21~",
            Qt.Key_F11: b"\x1b[23~", Qt.Key_F12: b"\x1b[24~",
        }
        if key in special:
            return special[key]
        if text:
            return text.encode("utf-8")
        return None

    # ---------- 滚轮 ----------
    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        lines = 3 if delta > 0 else -3
        hist = len(self.screen.history.top)
        self._scroll_offset = max(0, min(hist, self._scroll_offset + lines))
        self._update_scrollbar()
        self.viewport().update()

    def clear_scrollback(self) -> None:
        self.screen.history.top.clear()
        self.screen.history.bottom.clear()
        self._scroll_offset = 0
        self._update_scrollbar()
        self.viewport().update()

    def set_font(self, family: str, size: int) -> None:
        self._font = QFont(family, size)
        self._font.setStyleHint(QFont.Monospace)
        self._fm = QFontMetricsF(self._font)
        self._char_w = self._fm.horizontalAdvance("W")
        self._char_h = self._fm.height()
        self._recalc_grid()
