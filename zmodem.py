"""ZMODEM 协议核 (CRC-16 模式)。

实现 rz/sz 文件传输所需的帧编解码 + 收发状态机, 与终端数据流解耦:
  - ZModemReceiver: 服务器执行 `sz file` (下载到本地)。服务器是发送方, 我们是接收方。
  - ZModemSender:   服务器执行 `rz`      (从本地上传)。服务器是接收方, 我们是发送方。

设计取舍:
  - 只用 CRC-16 (ZRINIT 不声明 CANFC32), 避免 CRC-32 实现分歧, 与 lrzsz 全兼容。
  - 接收方 ZRINIT 声明 CANFDX|CANOVIO 且 rx buffer=0, 发送方以 ZCRCG 流式连续发送。

状态机通过 feed(bytes) 驱动, 通过 write_fn 回写应答, 通过回调报告进度/完成/错误。
协议帧格式参考 Chuck Forsberg 的 ZMODEM 规范。

诚实边界: 本模块经收发端回环测试验证协议自洽 (CRC/转义/分帧/握手/数据完整性),
与真实 lrzsz 的互通需在真实服务器上实测。
"""
from __future__ import annotations

from typing import Callable, Optional

# ---- 帧类型 ----
ZRQINIT = 0
ZRINIT = 1
ZSINIT = 2
ZACK = 3
ZFILE = 4
ZSKIP = 5
ZNAK = 6
ZABORT = 7
ZFIN = 8
ZRPOS = 9
ZDATA = 10
ZEOF = 11
ZFERR = 12
ZCRC = 13
ZCHALLENGE = 14
ZCOMPL = 15
ZCAN = 16
ZFREECNT = 17
ZCOMMAND = 18
ZSTDERR = 19

# ---- 特殊字节 ----
ZPAD = 0x2A       # '*'
ZDLE = 0x18       # CAN, ZMODEM 转义引导符
ZDLEE = 0x58      # ZDLE 经转义后的值 (0x18 ^ 0x40)
ZBIN = 0x41       # 'A' 二进制头, CRC-16
ZHEX = 0x42       # 'B' 十六进制头
ZBIN32 = 0x43     # 'C' 二进制头, CRC-32
XON = 0x11
XOFF = 0x13

# ---- 数据子包结束标记 ----
ZCRCE = 0x68      # 'h' 帧结束, 后随头
ZCRCG = 0x69      # 'i' 帧继续, 不停顿
ZCRCQ = 0x6A      # 'j' 帧继续, 期待 ZACK
ZCRCW = 0x6B      # 'k' 帧结束, 期待 ZACK

# ---- ZDLE 转义特例 ----
ZRUB0 = 0x6C      # 'l' -> 0x7F
ZRUB1 = 0x6D      # 'm' -> 0xFF

# ---- ZFILE 转换选项 (ZF0) ----
ZCBIN = 1         # 二进制传输, 不做换行转换
ZCNL = 2          # ASCII, 转换换行
ZCRESUM = 3       # 断点续传

# ---- ZFILE 管理选项 (ZF1, 低 5 位, ZMMASK=037) ----
ZMNEWL = 1        # 源更新或长度不同才传
ZMCRC = 2         # 源与目标 CRC 不同才传
ZMAPND = 3        # 追加到已存在文件
ZMCLOB = 4        # 覆盖已存在文件
ZMNEW = 5         # 源更新才传
ZMDIFF = 6        # 日期或长度不同才传
ZMPROT = 7        # 保护: 目标已存在则拒收 (不覆盖)
ZMCHNG = 8        # 目标已存在则改名
ZMMASK = 0o37     # 管理选项 5 位掩码

# ---- ZRINIT 能力标志 (ZF0) ----
CANFDX = 0x01     # 全双工
CANOVIO = 0x02    # 可在磁盘 I/O 时接收
CANBRK = 0x04
CANFC32 = 0x20    # 支持 CRC-32 (本实现不声明)

# 需 ZDLE 转义的字节 (控制字符 + 流控敏感位)
_ESCAPE = {ZDLE, 0x10, XON, XOFF, 0x90, 0x91, 0x93}


def crc16(data: bytes, crc: int = 0) -> int:
    """ZMODEM/XMODEM CRC-16: poly 0x1021, init 0, 无反射。"""
    for b in data:
        crc ^= (b << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc & 0xFFFF


def zdle_encode(data: bytes) -> bytes:
    """对数据做 ZDLE 转义。@<CR> 特例按前一字节判定。"""
    out = bytearray()
    prev = 0
    for b in data:
        if b in _ESCAPE:
            out.append(ZDLE)
            out.append(b ^ 0x40)
        elif (b == 0x0D or b == 0x8D) and (prev & 0x7F) == 0x40:
            out.append(ZDLE)
            out.append(b ^ 0x40)
        else:
            out.append(b)
        prev = b
    return bytes(out)


def cancel_sequence() -> bytes:
    """取消传输: 8x CAN + 8x 退格 (清行)。"""
    return bytes([ZDLE] * 8) + bytes([0x08] * 8)


def _pos4(pos: int) -> tuple:
    return (pos & 0xFF, (pos >> 8) & 0xFF, (pos >> 16) & 0xFF, (pos >> 24) & 0xFF)


def _unpos4(p: tuple) -> int:
    return p[0] | (p[1] << 8) | (p[2] << 16) | (p[3] << 24)


def make_hex_header(ftype: int, p=(0, 0, 0, 0)) -> bytes:
    payload = bytes([ftype, p[0], p[1], p[2], p[3]])
    crc = crc16(payload)
    body = payload + bytes([(crc >> 8) & 0xFF, crc & 0xFF])
    out = bytearray([ZPAD, ZPAD, ZDLE, ZHEX])
    out += body.hex().encode("ascii")
    out += b"\r\n"
    if ftype not in (ZACK, ZFIN):
        out.append(XON)
    return bytes(out)


def make_bin_header(ftype: int, p=(0, 0, 0, 0)) -> bytes:
    payload = bytes([ftype, p[0], p[1], p[2], p[3]])
    crc = crc16(payload)
    body = payload + bytes([(crc >> 8) & 0xFF, crc & 0xFF])
    return bytes([ZPAD, ZDLE, ZBIN]) + zdle_encode(body)


def make_data_subpacket(data: bytes, frameend: int) -> bytes:
    crc = crc16(data + bytes([frameend]))
    return (zdle_encode(data) + bytes([ZDLE, frameend])
            + zdle_encode(bytes([(crc >> 8) & 0xFF, crc & 0xFF])))


# ---- 流式解析 (处理任意分块到达的字节) ----

def _dezdle_read(buf: bytes, i: int, count: int):
    """从 buf[i:] 读取 count 个逻辑字节 (ZDLE 解码)。返回 (bytes, new_i) 或 None(不完整)。"""
    out = bytearray()
    while len(out) < count:
        if i >= len(buf):
            return None
        b = buf[i]
        if b == ZDLE:
            if i + 1 >= len(buf):
                return None
            c = buf[i + 1]
            if c == ZRUB0:
                out.append(0x7F)
            elif c == ZRUB1:
                out.append(0xFF)
            else:
                out.append(c ^ 0x40)
            i += 2
        else:
            out.append(b)
            i += 1
    return bytes(out), i


class _Parsed:
    """解析结果: 头 或 数据子包。"""
    __slots__ = ("kind", "ftype", "pos", "flags", "data", "frameend", "consumed", "ok")

    def __init__(self, kind, consumed):
        self.kind = kind          # 'header' | 'subpacket' | 'oo'
        self.consumed = consumed
        self.ftype = None
        self.pos = 0
        self.flags = 0
        self.data = b""
        self.frameend = None
        self.ok = True


def parse_header(buf: bytes):
    """在 buf 中查找并解析一个 ZMODEM 头。
    返回 _Parsed(kind='header') / None(不完整) / ('garbage', n) 跳过 n 字节。
    """
    n = len(buf)
    i = 0
    while i < n:
        if buf[i] == ZDLE and i + 1 < n:
            fmt = buf[i + 1]
            if fmt == ZHEX:
                start = i + 2
                # 需要 14 个 hex 字符
                if start + 14 > n:
                    return None
                hexpart = buf[start:start + 14]
                try:
                    body = bytes.fromhex(hexpart.decode("ascii"))
                except ValueError:
                    i += 1
                    continue
                j = start + 14
                # 跳过尾随 CR/LF/XON
                while j < n and buf[j] in (0x0D, 0x0A, XON, 0x8D, 0x8A):
                    j += 1
                    if j - (start + 14) >= 3:
                        break
                return _finish_header(body, j)
            elif fmt == ZBIN:
                r = _dezdle_read(buf, i + 2, 7)
                if r is None:
                    return None
                body, j = r
                return _finish_header(body, j)
            elif fmt == ZBIN32:
                r = _dezdle_read(buf, i + 2, 9)
                if r is None:
                    return None
                body, j = r
                # CRC-32 头: 校验略 (本实现不声明 CANFC32, 通常不会收到)
                p = _Parsed("header", j)
                p.ftype = body[0]
                p.pos = _unpos4((body[1], body[2], body[3], body[4]))
                p.flags = body[4]
                return p
            else:
                i += 1
        else:
            i += 1
    # 未找到头起始; 若结尾可能是半个 ZDLE 序列则保留
    if n > 0 and buf[-1] == ZDLE:
        return ("garbage", n - 1)
    return ("garbage", n)


def _finish_header(body: bytes, consumed: int):
    ftype = body[0]
    p4 = (body[1], body[2], body[3], body[4])
    crc = crc16(body[0:5])
    exp = (body[5] << 8) | body[6]
    p = _Parsed("header", consumed)
    p.ftype = ftype
    p.pos = _unpos4(p4)
    p.flags = body[4]   # ZF0 在 wire[4]
    p.ok = (crc == exp)
    return p


def parse_subpacket(buf: bytes, i: int = 0):
    """从 buf[i:] 解析一个数据子包。返回 _Parsed(kind='subpacket') 或 None(不完整)。"""
    data = bytearray()
    n = len(buf)
    while True:
        if i >= n:
            return None
        b = buf[i]
        if b == ZDLE:
            if i + 1 >= n:
                return None
            c = buf[i + 1]
            if c in (ZCRCE, ZCRCG, ZCRCQ, ZCRCW):
                frameend = c
                r = _dezdle_read(buf, i + 2, 2)
                if r is None:
                    return None
                crcb, j = r
                p = _Parsed("subpacket", j)
                p.data = bytes(data)
                p.frameend = frameend
                exp = (crcb[0] << 8) | crcb[1]
                p.ok = (crc16(bytes(data) + bytes([frameend])) == exp)
                return p
            elif c == ZRUB0:
                data.append(0x7F)
                i += 2
            elif c == ZRUB1:
                data.append(0xFF)
                i += 2
            else:
                data.append(c ^ 0x40)
                i += 2
        else:
            data.append(b)
            i += 1


# ============================================================
#  接收方: 服务器 sz -> 本地下载
# ============================================================
class ZModemReceiver:
    """服务器执行 sz, 我们接收文件。

    回调:
      write_fn(bytes)                    : 回写到连接
      on_file_meta(name, size) -> path   : 收到文件元信息, 返回本地保存路径 (None=跳过)
      on_progress(name, recv, size)      : 进度
      on_done(name, path)                : 单文件完成
      on_finish()                        : 整个会话结束
      on_error(msg)                      : 出错
    """
    S_INIT = 0        # 已发 ZRINIT, 等 ZFILE
    S_DATA = 1        # 收 ZDATA/数据子包
    S_DONE = 2

    def __init__(self, write_fn: Callable[[bytes], None],
                 on_file_meta: Callable[[str, int], Optional[str]],
                 on_progress=None, on_done=None, on_finish=None, on_error=None):
        self.write_fn = write_fn
        self.on_file_meta = on_file_meta
        self.on_progress = on_progress or (lambda *a: None)
        self.on_done = on_done or (lambda *a: None)
        self.on_finish = on_finish or (lambda: None)
        self.on_error = on_error or (lambda m: None)

        self._buf = bytearray()
        self._state = self.S_INIT
        self._expect = "header"     # header | subpacket
        self._fp = None
        self._path = None
        self._name = None
        self._size = 0
        self._recv = 0
        self._skip = False
        self.finished = False

    def start(self):
        """已探测到 sz 启动, 主动发 ZRINIT。"""
        self.write_fn(make_hex_header(ZRINIT, (0, 0, 0, CANFDX | CANOVIO)))

    def feed(self, data: bytes):
        if self.finished:
            return
        self._buf += data
        try:
            self._pump()
        except Exception as e:  # 防御: 协议异常不崩溃调用方
            self._fail("协议异常: %s" % e)

    def _pump(self):
        while self._buf and not self.finished:
            # 检测 "OO" (over and out)
            if self._state == self.S_DONE:
                if self._buf[:2] == b"OO":
                    del self._buf[:2]
                self._complete()
                return
            if self._expect == "header":
                res = parse_header(bytes(self._buf))
                if res is None:
                    return
                if isinstance(res, tuple):     # garbage
                    _, nskip = res
                    if nskip <= 0:
                        return
                    del self._buf[:nskip]
                    continue
                del self._buf[:res.consumed]
                if not res.ok:
                    # 头 CRC 错, 请求重发
                    self.write_fn(make_hex_header(ZNAK))
                    continue
                self._on_header(res)
            else:  # subpacket
                res = parse_subpacket(bytes(self._buf))
                if res is None:
                    return
                del self._buf[:res.consumed]
                self._on_subpacket(res)

    def _on_header(self, h: _Parsed):
        ft = h.ftype
        if ft == ZRQINIT:
            self.write_fn(make_hex_header(ZRINIT, (0, 0, 0, CANFDX | CANOVIO)))
        elif ft == ZFILE:
            self._expect = "subpacket"     # ZFILE 后随元信息子包
        elif ft == ZDATA:
            self._recv = h.pos
            self._expect = "subpacket"
        elif ft == ZEOF:
            self._close_file()
            # 准备接收下一个文件
            self._expect = "header"
            self.write_fn(make_hex_header(ZRINIT, (0, 0, 0, CANFDX | CANOVIO)))
        elif ft == ZFIN:
            self.write_fn(make_hex_header(ZFIN))
            self._state = self.S_DONE
            self._expect = "header"
        elif ft == ZSKIP:
            self._expect = "header"
        else:
            # 其它头忽略
            pass

    def _on_subpacket(self, sp: _Parsed):
        if self._state == self.S_INIT:
            # ZFILE 元信息子包: "filename\0size mtime mode ...\0"
            self._parse_file_meta(sp.data)
            self._expect = "header"
            return
        # 数据子包
        if not sp.ok:
            # 数据 CRC 错, 请求从当前位置重发
            self.write_fn(make_hex_header(ZRPOS, _pos4(self._recv)))
            self._expect = "header"
            return
        if self._fp and not self._skip:
            self._fp.write(sp.data)
        self._recv += len(sp.data)
        self.on_progress(self._name, self._recv, self._size)

        fe = sp.frameend
        if fe == ZCRCG:
            self._expect = "subpacket"      # 连续
        elif fe == ZCRCQ:
            self.write_fn(make_hex_header(ZACK, _pos4(self._recv)))
            self._expect = "subpacket"
        elif fe == ZCRCE:
            self._expect = "header"         # 帧结束, 等 ZEOF
        elif fe == ZCRCW:
            self.write_fn(make_hex_header(ZACK, _pos4(self._recv)))
            self._expect = "header"

    def _parse_file_meta(self, data: bytes):
        try:
            nul = data.index(0)
        except ValueError:
            nul = len(data)
        name = data[:nul].decode("utf-8", "replace")
        rest = data[nul + 1:].split(b"\0")[0].decode("ascii", "replace").strip()
        size = 0
        if rest:
            parts = rest.split()
            if parts and parts[0].isdigit():
                size = int(parts[0])
        self._name = name
        self._size = size
        self._recv = 0
        self._skip = False
        path = self.on_file_meta(name, size)
        if path is None:
            self._skip = True
            self._fp = None
            self._path = None
            self.write_fn(make_hex_header(ZSKIP))
            return
        self._path = path
        try:
            self._fp = open(path, "wb")
        except Exception as e:
            self._fail("无法写入 %s: %s" % (path, e))
            return
        self._state = self.S_DATA
        # 请求从 0 开始发送数据
        self.write_fn(make_hex_header(ZRPOS, _pos4(0)))

    def _close_file(self):
        if self._fp:
            try:
                self._fp.close()
            except Exception:
                pass
            self.on_done(self._name, self._path)
        self._fp = None
        self._state = self.S_INIT

    def _complete(self):
        self.finished = True
        self.on_finish()

    def _fail(self, msg: str):
        try:
            self.write_fn(cancel_sequence())
        except Exception:
            pass
        if self._fp:
            try:
                self._fp.close()
            except Exception:
                pass
            self._fp = None
        self.finished = True
        self.on_error(msg)


# ============================================================
#  发送方: 服务器 rz -> 本地上传
# ============================================================
class ZModemSender:
    """服务器执行 rz, 我们发送本地文件。

    回调:
      write_fn(bytes)               : 回写到连接
      on_progress(name, sent, size) : 进度
      on_done(name)                 : 单文件完成
      on_finish()                   : 会话结束
      on_error(msg)                 : 出错
    """
    CHUNK = 8192

    S_WAIT_RINIT = 0
    S_WAIT_RPOS = 1
    S_SENDING = 2
    S_WAIT_FIN = 3

    def __init__(self, write_fn: Callable[[bytes], None], files,
                 on_progress=None, on_done=None, on_finish=None, on_error=None,
                 on_skip=None):
        """files: [(local_path, remote_name, size, opener), ...]
        opener() -> file-like (rb)。单文件可传 [(path, name, size, open_fn)]。
        """
        self.write_fn = write_fn
        self.files = list(files)
        self.on_progress = on_progress or (lambda *a: None)
        self.on_done = on_done or (lambda *a: None)
        self.on_finish = on_finish or (lambda: None)
        self.on_error = on_error or (lambda m: None)
        self.on_skip = on_skip or (lambda *a: None)

        self._buf = bytearray()
        self._state = self.S_WAIT_RINIT
        self._idx = -1
        self._fp = None
        self._name = None
        self._path = None
        self._size = 0
        self._sent = 0
        self.finished = False

    def start(self):
        """已探测到 rz 启动 (收到 ZRINIT)。也可主动发 ZRQINIT。"""
        self.write_fn(make_hex_header(ZRQINIT))

    def feed(self, data: bytes):
        if self.finished:
            return
        self._buf += data
        try:
            self._pump()
        except Exception as e:
            self._fail("协议异常: %s" % e)

    def _pump(self):
        while self._buf and not self.finished:
            res = parse_header(bytes(self._buf))
            if res is None:
                return
            if isinstance(res, tuple):
                _, nskip = res
                if nskip <= 0:
                    return
                del self._buf[:nskip]
                continue
            del self._buf[:res.consumed]
            if not res.ok:
                continue
            self._on_header(res)

    def _on_header(self, h: _Parsed):
        ft = h.ftype
        if ft == ZRINIT:
            if self._state in (self.S_WAIT_RINIT,):
                self._send_next_file()
            elif self._state == self.S_WAIT_FIN:
                # 上一个文件已 EOF, 接收方就绪 -> 无更多文件则 ZFIN
                self._send_next_file()
        elif ft == ZRPOS:
            if self._state == self.S_WAIT_RPOS or self._state == self.S_SENDING:
                self._sent = h.pos
                if self._fp:
                    self._fp.seek(h.pos)
                self._state = self.S_SENDING
                self._send_data()
        elif ft == ZACK:
            pass  # 流控 ACK, 连续模式下忽略
        elif ft == ZSKIP:
            # 远端已存在同名文件(ZMPROT 保护)-> 跳过, 不计入完成
            name, path = self._name, self._path
            self._close_file(done=False)
            self.on_skip(name, path)
            self._send_next_file()
        elif ft == ZFIN:
            self.write_fn(b"OO")
            self._complete()
        elif ft == ZNAK:
            # 重发当前文件的 ZFILE
            if self._fp is not None:
                self._send_zfile()

    def _send_next_file(self):
        self._idx += 1
        if self._idx >= len(self.files):
            self.write_fn(make_hex_header(ZFIN))
            self._state = self.S_WAIT_FIN
            return
        path, name, size, opener = self.files[self._idx]
        try:
            self._fp = opener()
        except Exception as e:
            self._fail("无法读取 %s: %s" % (path, e))
            return
        self._name = name
        self._path = path
        self._size = size
        self._sent = 0
        self._send_zfile()
        self._state = self.S_WAIT_RPOS

    def _send_zfile(self):
        # ZF1(p[2])=ZMPROT: 远端已存在则拒收(回 ZSKIP); ZF0(p[3])=ZCBIN: 二进制传输
        self.write_fn(make_bin_header(ZFILE, (0, 0, ZMPROT, ZCBIN)))
        info = (self._name.encode("utf-8") + b"\0"
                + ("%d 0 100644 0 1 %d" % (self._size, self._size)).encode("ascii")
                + b"\0")
        self.write_fn(make_data_subpacket(info, ZCRCW))

    def _send_data(self):
        # 连续流式发送整个文件
        self.write_fn(make_bin_header(ZDATA, _pos4(self._sent)))
        while True:
            chunk = self._fp.read(self.CHUNK)
            if not chunk:
                # 文件结束
                self.write_fn(make_data_subpacket(b"", ZCRCE))
                self.write_fn(make_bin_header(ZEOF, _pos4(self._sent)))
                self._close_file(done=True)
                self._state = self.S_WAIT_FIN
                return
            self._sent += len(chunk)
            more = len(chunk) == self.CHUNK
            # 预读判断是否最后一块
            self.write_fn(make_data_subpacket(chunk, ZCRCG))
            self.on_progress(self._name, self._sent, self._size)
            if not more:
                self.write_fn(make_data_subpacket(b"", ZCRCE))
                self.write_fn(make_bin_header(ZEOF, _pos4(self._sent)))
                self._close_file(done=True)
                self._state = self.S_WAIT_FIN
                return

    def _close_file(self, done=False):
        if self._fp:
            try:
                self._fp.close()
            except Exception:
                pass
            if done and self._name:
                self.on_done(self._name, self._path)
        self._fp = None

    def _complete(self):
        self.finished = True
        self.on_finish()

    def _fail(self, msg: str):
        try:
            self.write_fn(cancel_sequence())
        except Exception:
            pass
        self._close_file()
        self.finished = True
        self.on_error(msg)


# ---- 探测: 从终端数据流中识别 ZMODEM 启动 ----
# sz 发送 ZRQINIT 十六进制头: "**\x18B00..."
# rz 发送 ZRINIT  十六进制头: "**\x18B01..."
_ZRQINIT_HEX = b"**\x18B00"
_ZRINIT_HEX = b"**\x18B01"


def detect_zmodem(buf: bytes):
    """在终端字节流中探测 ZMODEM 启动序列。
    返回 ('download', idx) / ('upload', idx) / None。idx 为启动头在 buf 中的起始下标。
    """
    i = buf.find(b"\x18B0")
    if i < 0:
        return None
    # 回退到 ZPAD 起始
    start = i
    while start > 0 and buf[start - 1] == ZPAD:
        start -= 1
    tag = buf[i + 3:i + 4]
    if tag == b"0":       # ZRQINIT -> 服务器要发送 -> 我们下载
        return ("download", start)
    if tag == b"1":       # ZRINIT -> 服务器要接收 -> 我们上传
        return ("upload", start)
    return None
