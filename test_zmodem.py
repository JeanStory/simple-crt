"""ZMODEM 协议核回环测试: Sender <-> Receiver 交叉接线验证。"""
import io
import os
import tempfile

import zmodem as Z


class Wire:
    """双向管道: a.write -> b.feed, 支持逐字节/分块投递测试分帧鲁棒性。"""
    def __init__(self, chunk=None):
        self.chunk = chunk       # None=整块; int=按大小切分投递
        self.a = None
        self.b = None
        self._qa = bytearray()   # a 要发给 b
        self._qb = bytearray()

    def wire(self, a, b):
        self.a, self.b = a, b

    def a_write(self, data):
        self._qa += data

    def b_write(self, data):
        self._qb += data

    def deliver(self):
        """把队列里的数据投递给对端, 直到双方都静默。"""
        for _ in range(100000):
            if not self._qa and not self._qb:
                return
            if self._qa:
                data = bytes(self._qa)
                self._qa.clear()
                self._feed(self.b, data)
            if self._qb:
                data = bytes(self._qb)
                self._qb.clear()
                self._feed(self.a, data)

    def _feed(self, target, data):
        if self.chunk is None:
            target.feed(data)
        else:
            for i in range(0, len(data), self.chunk):
                target.feed(data[i:i + self.chunk])


def run_transfer(payload: bytes, name="test.bin", chunk=None):
    """模拟: 我们(Sender)上传 -> 对端(Receiver)接收。返回接收到的字节。"""
    tmpdir = tempfile.mkdtemp()
    recv_holder = {}

    wire = Wire(chunk=chunk)

    # Sender = 我们上传
    def opener():
        return io.BytesIO(payload)
    sender = Z.ZModemSender(
        write_fn=wire.a_write,
        files=[("local", name, len(payload), opener)],
    )

    # Receiver = 对端接收, 写到临时文件
    def on_meta(fname, size):
        return os.path.join(tmpdir, fname)
    def on_done(fname, path):
        recv_holder["path"] = path
    receiver = Z.ZModemReceiver(
        write_fn=wire.b_write,
        on_file_meta=on_meta,
        on_done=on_done,
    )

    wire.wire(sender, receiver)
    # 握手: receiver 先发 ZRINIT (模拟 rz 启动), sender 收到后开始
    receiver.start()          # rz -> ZRINIT
    wire.deliver()
    # 触发 sender 起步 (它也可主动 ZRQINIT, 但这里 ZRINIT 已足够)
    sender.start()
    wire.deliver()

    assert sender.finished, "sender 未结束, state=%s" % sender._state
    path = recv_holder.get("path")
    assert path, "receiver 未完成文件"
    with open(path, "rb") as f:
        return f.read()


def run_download(payload: bytes, name="dl.bin", chunk=None):
    """模拟: 对端(Sender/sz)发送 -> 我们(Receiver)下载。返回接收字节。"""
    tmpdir = tempfile.mkdtemp()
    recv_holder = {}
    wire = Wire(chunk=chunk)

    def opener():
        return io.BytesIO(payload)
    # 对端是 sender (sz)
    server_sender = Z.ZModemSender(
        write_fn=wire.b_write,   # 对端写 -> 我们收
        files=[("f", name, len(payload), opener)],
    )

    def on_meta(fname, size):
        return os.path.join(tmpdir, fname)
    def on_done(fname, path):
        recv_holder["path"] = path
    # 我们是 receiver
    me = Z.ZModemReceiver(
        write_fn=wire.a_write,
        on_file_meta=on_meta,
        on_done=on_done,
    )

    wire.wire(me, server_sender)
    # sz 启动: 服务器发 ZRQINIT -> 我们探测到 -> 发 ZRINIT
    server_sender.start()        # ZRQINIT (走 b_write -> me)
    wire.deliver()
    me.start()                   # 兜底再发一次 ZRINIT
    wire.deliver()

    assert server_sender.finished, "server sender 未结束 state=%s" % server_sender._state
    path = recv_holder.get("path")
    assert path, "未下载到文件"
    with open(path, "rb") as f:
        return f.read()


# ---------------- 单元: CRC / 转义 ----------------
def test_crc16_known():
    # CRC-16/XMODEM("123456789") = 0x31C3
    assert Z.crc16(b"123456789") == 0x31C3

def test_zdle_roundtrip():
    for raw in [bytes(range(256)), b"hello", b"\x18\x10\x11\x13", b"@\r", b"a@\rb"]:
        enc = Z.zdle_encode(raw)
        # 用 subpacket 解码路径验证 (末尾附 ZCRCE + crc)
        crc = Z.crc16(raw + bytes([Z.ZCRCE]))
        pkt = enc + bytes([Z.ZDLE, Z.ZCRCE]) + Z.zdle_encode(bytes([(crc >> 8) & 0xFF, crc & 0xFF]))
        sp = Z.parse_subpacket(pkt)
        assert sp is not None and sp.ok, "roundtrip fail for %r" % raw
        assert sp.data == raw, "data mismatch %r != %r" % (sp.data, raw)

def test_header_roundtrip():
    for mk in (Z.make_hex_header, Z.make_bin_header):
        h = mk(Z.ZRPOS, Z._pos4(123456))
        p = Z.parse_header(h)
        assert not isinstance(p, tuple) and p is not None
        assert p.ok and p.ftype == Z.ZRPOS and p.pos == 123456

def test_detect():
    assert Z.detect_zmodem(b"noise**\x18B00000000")[0] == "download"
    assert Z.detect_zmodem(b"rz\r\n**\x18B0100000000")[0] == "upload"
    assert Z.detect_zmodem(b"just terminal text") is None


# ---------------- 集成: 传输 ----------------
def test_upload_small():
    data = b"Hello ZMODEM upload!\n"
    assert run_transfer(data) == data

def test_upload_binary_all_bytes():
    data = bytes(range(256)) * 40    # 含所有需转义字节
    assert run_transfer(data) == data

def test_upload_large():
    data = os.urandom(100000)        # 跨多个 8K chunk
    assert run_transfer(data) == data

def test_upload_chunked_wire():
    data = os.urandom(50000)
    # 模拟字节流被网络切成小块到达
    assert run_transfer(data, chunk=7) == data

def test_upload_empty():
    assert run_transfer(b"") == b""

def test_download_small():
    data = b"downloaded content 123\n"
    assert run_download(data) == data

def test_download_large_chunked():
    data = os.urandom(80000)
    assert run_download(data, chunk=13) == data


if __name__ == "__main__":
    import traceback
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print("PASS", t.__name__)
            passed += 1
        except Exception:
            print("FAIL", t.__name__)
            traceback.print_exc()
            failed += 1
    print("\n%d/%d passed" % (passed, passed + failed))
