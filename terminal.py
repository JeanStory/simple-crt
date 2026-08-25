"""终端控件: pyte 屏幕缓冲 + QPainter 渲染 + 键盘输入映射为 ANSI 序列。"""
from __future__ import annotations

import unicodedata

import pyte
from PySide6.QtCore import Qt, QTimer, Signal, QRect
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QPainter, QKeyEvent,
                           QGuiApplication, QAction)
from PySide6.QtWidgets import QWidget, QAbstractScrollArea, QMenu

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
SELECTION_BG = "#264f78"  # 选区高亮 (VS Code 风格蓝)


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


def _is_wide_char(ch: str) -> bool:
    """判断是否为东亚全角字符 (占 2 个终端列宽)。"""
    if not ch:
        return False
    return unicodedata.east_asian_width(ch[0]) in ("W", "F")


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
        # 强制优先轮廓字体, 避免 DirectWrite 回退到 Fixedsys/MS Sans Serif 等
        # 老式位图字体导致 CreateFontFaceFromHDC 失败刷屏警告。
        self._font.setStyleStrategy(QFont.PreferOutline)
        self._fm = QFontMetricsF(self._font)
        self._char_w = self._fm.horizontalAdvance("W")
        self._char_h = self._fm.height()

        self.cols = 80
        self.rows = 24
        self.screen = pyte.HistoryScreen(self.cols, self.rows, history=history, ratio=0.5)
        self.stream = pyte.ByteStream(self.screen)

        self._scroll_offset = 0  # 向上回滚的行数
        self._wheel_accum = 0.0  # 滚轮 delta 累积余数, 用于平滑滚动
        self._blink = True
        # 选区: 锚定绝对行号 (history 段 + 当前屏幕段的连续坐标)
        self._sel_anchor = None   # (abs_line, col) 起点
        self._sel_end = None      # (abs_line, col) 终点
        self._selecting = False
        self._blink_timer = QTimer(self)
        self._blink_timer.timeout.connect(self._toggle_blink)
        self._blink_timer.start(500)

        self.verticalScrollBar().valueChanged.connect(self._on_scroll)
        # 垂直滚动条常显, 水平不需要
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 深色主题滚动条样式 (默认样式在深色终端背景上几乎不可见)
        self.setStyleSheet(
            "QAbstractScrollArea{background:%s;}"
            "QScrollBar:vertical{background:#2b2b2b;width:14px;margin:0;}"
            "QScrollBar::handle:vertical{background:#5a5a5a;min-height:24px;"
            "border-radius:4px;margin:2px;}"
            "QScrollBar::handle:vertical:hover{background:#787878;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
            "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:none;}"
            % DEFAULT_BG
        )

    # ---------- 数据流 ----------
    def feed(self, data: bytes) -> None:
        # 排查用: 设环境变量 SCRT_TRACE=<日志文件路径> 后, 每段收到的原始字节
        # (含收前/收后光标位置)会以 repr 形式追加写入该文件。平时不设则零开销。
        import os as _os
        _trace = _os.environ.get("SCRT_TRACE")
        if _trace:
            try:
                _before = (self.screen.cursor.y, self.screen.cursor.x)
            except Exception:
                _before = None
            try:
                self.stream.feed(data)
            except Exception:
                pass
            try:
                _after = (self.screen.cursor.y, self.screen.cursor.x)
                _lnm = getattr(self.screen, "mode", None)
                with open(_trace, "a", encoding="utf-8") as _f:
                    _f.write("RAW=%r  cursor %s->%s  mode=%r\n"
                             % (data, _before, _after, _lnm))
            except Exception:
                pass
            self._scroll_offset = 0
            self._clear_selection()
            self._update_scrollbar()
            self.viewport().update()
            return
        try:
            self.stream.feed(data)
        except Exception:
            pass
        # 有新输出时跳回底部, 清除选区(绝对行锚会随历史滚动失效)
        self._scroll_offset = 0
        self._clear_selection()
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
        bar.setSingleStep(1)
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
        hist_len = len(hist_top)

        # 归一化选区 (abs_line, col) start<=end
        sel = self._normalized_selection()

        # 组合"历史 + 当前屏幕"的可视区
        for row in range(self.rows):
            src_index = row - self._scroll_offset
            # 该行在"历史+屏幕"连续坐标中的绝对行号
            abs_line = hist_len + src_index
            sel_range = self._row_selection_range(abs_line, sel)
            if src_index < 0:
                # 显示历史行
                h_idx = len(hist_top) + src_index
                if 0 <= h_idx < len(hist_top):
                    line = hist_top[h_idx]
                    self._draw_line(painter, row, line, is_history=True,
                                    sel_range=sel_range)
            else:
                line = buf[src_index]
                self._draw_line(painter, row, line, is_history=False,
                                sel_range=sel_range)

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

    def _draw_line(self, painter: QPainter, row: int, line, is_history: bool,
                   sel_range=None) -> None:
        y = row * self._char_h
        # 选区背景先整体铺一层 (含行尾空白, 视觉更接近真实终端)
        if sel_range is not None:
            s_col, e_col = sel_range  # [s_col, e_col) 半开
            if e_col > s_col:
                x0 = s_col * self._char_w
                w = (e_col - s_col) * self._char_w
                painter.fillRect(QRect(int(x0), int(y), int(w) + 1,
                                       int(self._char_h) + 1),
                                 QColor(SELECTION_BG))
        # line 是 dict-like: 列 -> Char
        max_col = self.cols
        for col in range(max_col):
            char = line[col]
            data = char.data or " "
            in_sel = sel_range is not None and sel_range[0] <= col < sel_range[1]
            if data == " " and char.bg == "default" and not char.reverse and not in_sel:
                continue
            fg = resolve_color(char.fg, DEFAULT_FG)
            bg = resolve_color(char.bg, DEFAULT_BG)
            if char.reverse:
                fg, bg = bg, fg
            x = col * self._char_w
            # 全角字符 (中文/日文/韩文等) 占 2 列宽, 绘制矩形需加倍, 否则字形右半被裁剪
            cell_span = 2 if _is_wide_char(data) else 1
            cell_w = self._char_w * cell_span
            rect = QRect(int(x), int(y), int(cell_w) + 1, int(self._char_h) + 1)
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

    # ---------- 选区 ----------
    def _clear_selection(self) -> None:
        self._sel_anchor = None
        self._sel_end = None
        self._selecting = False

    def _has_selection(self) -> bool:
        return self._sel_anchor is not None and self._sel_end is not None \
            and self._sel_anchor != self._sel_end

    def _normalized_selection(self):
        """返回 (start, end) 且 start<=end, 元素为 (abs_line, col)。无选区返回 None。"""
        if not self._has_selection():
            return None
        a, b = self._sel_anchor, self._sel_end
        if (a[0], a[1]) <= (b[0], b[1]):
            return a, b
        return b, a

    def _row_selection_range(self, abs_line: int, sel):
        """给定行的绝对行号, 返回该行被选中的列区间 [s_col, e_col) 或 None。"""
        if sel is None:
            return None
        (sl, sc), (el, ec) = sel
        if abs_line < sl or abs_line > el:
            return None
        s_col = sc if abs_line == sl else 0
        e_col = ec if abs_line == el else self.cols
        if e_col < s_col:
            return None
        return (s_col, e_col)

    def _pixel_to_cell(self, pos):
        """视口像素坐标 -> (abs_line, col), 做边界钳制。"""
        col = int(pos.x() / self._char_w)
        col = max(0, min(self.cols, col))
        row = int(pos.y() / self._char_h)
        row = max(0, min(self.rows - 1, row))
        hist_len = len(self.screen.history.top)
        abs_line = hist_len + (row - self._scroll_offset)
        return (abs_line, col)

    def _line_at_abs(self, abs_line: int):
        """按绝对行号取行对象 (历史段或当前屏幕段)。越界返回 None。"""
        hist_top = list(self.screen.history.top)
        hist_len = len(hist_top)
        src_index = abs_line - hist_len
        if src_index < 0:
            h_idx = hist_len + src_index
            if 0 <= h_idx < hist_len:
                return hist_top[h_idx]
            return None
        if 0 <= src_index < self.rows:
            return self.screen.buffer[src_index]
        return None

    def get_selected_text(self) -> str:
        sel = self._normalized_selection()
        if sel is None:
            return ""
        (sl, sc), (el, ec) = sel
        parts = []
        for abs_line in range(sl, el + 1):
            line = self._line_at_abs(abs_line)
            if line is None:
                parts.append("")
                continue
            s_col = sc if abs_line == sl else 0
            e_col = ec if abs_line == el else self.cols
            chars = [(line[c].data or " ") for c in range(s_col, min(e_col, self.cols))]
            parts.append("".join(chars).rstrip())
        return "\n".join(parts)

    # ---------- 鼠标 (选择/粘贴) ----------
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._sel_anchor = self._pixel_to_cell(event.position().toPoint())
            self._sel_end = self._sel_anchor
            self._selecting = True
            self.viewport().update()
        elif event.button() == Qt.MiddleButton:
            # X11 风格中键粘贴 (选区优先, 否则剪贴板)
            sel = self.get_selected_text()
            if sel:
                self._send_paste(sel)
            else:
                self.paste_clipboard()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._selecting:
            self._sel_end = self._pixel_to_cell(event.position().toPoint())
            self.viewport().update()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._selecting:
            self._sel_end = self._pixel_to_cell(event.position().toPoint())
            self._selecting = False
            # 选中即自动复制到剪贴板 (可选行为, 与多数终端一致)
            if self._has_selection():
                txt = self.get_selected_text()
                if txt:
                    QGuiApplication.clipboard().setText(txt)
            self.viewport().update()
        else:
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        # 双击选中光标下的单词
        if event.button() == Qt.LeftButton:
            abs_line, col = self._pixel_to_cell(event.position().toPoint())
            line = self._line_at_abs(abs_line)
            if line is not None:
                def is_word(c):
                    return c.isalnum() or c in "_-./"
                start = col
                while start > 0 and is_word(line[start - 1].data or " "):
                    start -= 1
                end = col
                while end < self.cols and is_word(line[end].data or " "):
                    end += 1
                if end > start:
                    self._sel_anchor = (abs_line, start)
                    self._sel_end = (abs_line, end)
                    txt = self.get_selected_text()
                    if txt:
                        QGuiApplication.clipboard().setText(txt)
                    self.viewport().update()
        else:
            super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        act_copy = QAction("复制", self)
        act_copy.setEnabled(self._has_selection())
        act_copy.triggered.connect(self.copy_selection)
        act_paste = QAction("粘贴", self)
        act_paste.setEnabled(bool(QGuiApplication.clipboard().text()))
        act_paste.triggered.connect(self.paste_clipboard)
        act_selall = QAction("全选", self)
        act_selall.triggered.connect(self.select_all)
        menu.addAction(act_copy)
        menu.addAction(act_paste)
        menu.addSeparator()
        menu.addAction(act_selall)
        menu.exec(event.globalPos())

    def select_all(self) -> None:
        hist_len = len(self.screen.history.top)
        self._sel_anchor = (0, 0)
        self._sel_end = (hist_len + self.rows - 1, self.cols)
        self.viewport().update()

    # ---------- 复制 / 粘贴 ----------
    def copy_selection(self) -> None:
        txt = self.get_selected_text()
        if txt:
            QGuiApplication.clipboard().setText(txt)

    def paste_clipboard(self) -> None:
        txt = QGuiApplication.clipboard().text()
        if txt:
            self._send_paste(txt)

    def _send_paste(self, text: str) -> None:
        """粘贴文本到终端: 规范化换行 + 支持 bracketed paste mode。"""
        # 统一换行为 \r (终端换行), 先折叠 \r\n 再单独 \n
        text = text.replace("\r\n", "\r").replace("\n", "\r")
        data = text.encode("utf-8")
        # pyte 记录了应用是否开启 bracketed paste (DECSET 2004)
        bracketed = bool(getattr(self.screen, "mode", set()) and
                         2004 in self.screen.mode)
        if bracketed:
            data = b"\x1b[200~" + data + b"\x1b[201~"
        self._scroll_offset = 0
        self.send_data.emit(data)

    # ---------- 键盘输入 ----------
    def event(self, event) -> bool:
        from PySide6.QtCore import QEvent
        et = event.type()
        # 终端必须独占键盘输入。Qt 在 KeyPress 之前会先发 ShortcutOverride 事件,
        # 询问是否让某个按键组合走应用级快捷键(菜单 QAction / 全局 QShortcut)。
        # 若不在此阶段抢下, 像 Ctrl+B(screen)、Ctrl+D、Ctrl+W 这类组合就可能被
        # 父级 QMainWindow 的快捷键系统吞掉, KeyPress 永远到不了 keyPressEvent,
        # 导致 screen/tmux/vim 等程序的控制键失效。这里对所有按键 accept 该事件,
        # 强制 Qt 把它作为普通 KeyPress 派发给终端。
        if et == QEvent.ShortcutOverride:
            # Alt+数字 保留给应用级标签切换快捷键(QShortcut), 不在此抢下。
            # 其余组合(Ctrl+B/D/W 等)仍强制交终端, 保证 screen/tmux/vim 可用。
            if (event.modifiers() & Qt.AltModifier) and Qt.Key_0 <= event.key() <= Qt.Key_9:
                return super().event(event)
            event.accept()
            return True
        # Qt 默认把 Tab/Backtab 当作焦点切换在 keyPressEvent 之前拦截,
        # 导致终端收不到 Tab(焦点跳走 => 看起来卡住)。这里拦下交给 keyPressEvent。
        if et == QEvent.KeyPress and event.key() in (Qt.Key_Tab, Qt.Key_Backtab):
            self.keyPressEvent(event)
            return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        mod = event.modifiers()
        text = event.text()

        # 复制/粘贴快捷键 (终端里 Ctrl+C 是 SIGINT, 故复制用 Ctrl+Shift+C)
        ctrl = mod & Qt.ControlModifier
        shift = mod & Qt.ShiftModifier
        if ctrl and shift and key == Qt.Key_C:
            self.copy_selection()
            event.accept()
            return
        if ctrl and shift and key == Qt.Key_V:
            self.paste_clipboard()
            event.accept()
            return
        if shift and key == Qt.Key_Insert:
            self.paste_clipboard()
            event.accept()
            return

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
        hist = len(self.screen.history.top)
        # 优先用 pixelDelta (触控板/高精度设备), 回退到 angleDelta (普通滚轮)
        pixel = event.pixelDelta().y()
        if pixel != 0:
            # 像素级: 按字符行高换算, 累积不足一行的余数
            self._wheel_accum += pixel / self._char_h
        else:
            # 角度级: 标准滚轮一格 120 单位, 映射为 3 行, 按实际 delta 比例累积
            angle = event.angleDelta().y()
            self._wheel_accum += angle / 120.0 * 3.0
        # 取累积的整数行部分, 保留小数余数给下次事件 (平滑)
        lines = int(self._wheel_accum)
        if lines != 0:
            self._wheel_accum -= lines
            # 向上滚(delta正)增大 offset, 向下滚减小
            self._scroll_offset = max(0, min(hist, self._scroll_offset + lines))
            self._update_scrollbar()
            self.viewport().update()
        event.accept()

    def clear_scrollback(self) -> None:
        self.screen.history.top.clear()
        self.screen.history.bottom.clear()
        self._scroll_offset = 0
        self._update_scrollbar()
        self.viewport().update()

    def set_font(self, family: str, size: int) -> None:
        self._font = QFont(family, size)
        self._font.setStyleHint(QFont.Monospace)
        self._font.setStyleStrategy(QFont.PreferOutline)
        self._fm = QFontMetricsF(self._font)
        self._char_w = self._fm.horizontalAdvance("W")
        self._char_h = self._fm.height()
        self._recalc_grid()
