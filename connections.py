"""连接后端: SSH / Serial / 本地进程。

统一接口 Connection:
  - start(cols, rows): 建立连接
  - write(data: bytes): 发送数据
  - resize(cols, rows): 通知远端窗口大小变化
  - close(): 关闭
  - on_data / on_close: 回调(在后台线程中触发, 调用方需保证线程安全)
"""
from __future__ import annotations

import base64
import hashlib
import os
import threading
import time
from typing import Callable, Optional


def _ssh_fingerprint(key) -> str:
    """返回 OpenSSH 风格的 SHA256 指纹 (SHA256:<base64>, 无 padding)。"""
    digest = hashlib.sha256(key.asbytes()).digest()
    b64 = base64.b64encode(digest).decode("ascii").rstrip("=")
    return "SHA256:" + b64


def _app_known_hosts_path() -> str:
    """应用自有的 known_hosts 文件 (~/.simple_crt/known_hosts)。"""
    return os.path.join(os.path.expanduser("~"), ".simple_crt", "known_hosts")


class Connection:
    """连接基类。数据到达时调用 on_data(bytes), 连接断开时调用 on_close(reason)。"""

    def __init__(self) -> None:
        self.on_data: Optional[Callable[[bytes], None]] = None
        self.on_close: Optional[Callable[[str], None]] = None
        self._alive = False
        self._reader: Optional[threading.Thread] = None

    # --- 子类需实现 ---
    def start(self, cols: int, rows: int) -> None:
        raise NotImplementedError

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def resize(self, cols: int, rows: int) -> None:
        pass

    def _do_close(self) -> None:
        pass

    # --- 通用 ---
    @property
    def alive(self) -> bool:
        return self._alive

    def _emit_data(self, data: bytes) -> None:
        if self.on_data:
            self.on_data(data)

    def _emit_close(self, reason: str) -> None:
        if self._alive:
            self._alive = False
            if self.on_close:
                self.on_close(reason)

    def close(self) -> None:
        self._alive = False
        try:
            self._do_close()
        except Exception:
            pass


class SSHConnection(Connection):
    """基于 paramiko 的交互式 SSH shell。"""

    def __init__(self, host: str, port: int = 22, username: str = "",
                 password: str = "", key_path: str = "", term: str = "xterm-256color") -> None:
        super().__init__()
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.key_path = key_path
        self.term = term
        self._client = None
        self._chan = None
        # 主机密钥校验回调: (host, keytype, fingerprint, changed) -> bool
        # 返回 True 表示用户信任并接受该密钥。为 None 时安全默认=拒绝未知密钥。
        self.host_key_verifier: Optional[Callable[[str, str, str, bool], bool]] = None

    def _persist_host_key(self, client, hostname, key) -> None:
        """把已被用户接受的主机密钥写入应用自有的 known_hosts。"""
        kh_path = _app_known_hosts_path()
        try:
            os.makedirs(os.path.dirname(kh_path), exist_ok=True)
            client.get_host_keys().add(hostname, key.get_name(), key)
            client.save_host_keys(kh_path)
        except Exception:
            pass  # 写盘失败不影响本次连接, 只是下次仍会再问

    def start(self, cols: int, rows: int) -> None:
        import paramiko

        client = paramiko.SSHClient()
        # 加载已知主机: 系统 ~/.ssh/known_hosts (只读) + 应用自有 (可写)
        try:
            client.load_system_host_keys()
        except Exception:
            pass
        kh_path = _app_known_hosts_path()
        if os.path.exists(kh_path):
            try:
                client.load_host_keys(kh_path)
            except Exception:
                pass

        conn = self

        class _InteractivePolicy(paramiko.MissingHostKeyPolicy):
            """未知主机 -> 询问用户 (TOFU)。拒绝则中止连接。"""
            def missing_host_key(self, cli, hostname, key):
                fp = _ssh_fingerprint(key)
                accept = False
                if conn.host_key_verifier is not None:
                    try:
                        accept = bool(conn.host_key_verifier(
                            hostname, key.get_name(), fp, False))
                    except Exception:
                        accept = False
                if not accept:
                    raise paramiko.SSHException(
                        f"用户拒绝了主机 {hostname} 的密钥 (指纹 {fp})")
                conn._persist_host_key(cli, hostname, key)

        client.set_missing_host_key_policy(_InteractivePolicy())

        connect_kwargs = dict(
            hostname=self.host,
            port=self.port,
            username=self.username,
            timeout=15,
            allow_agent=True,
            look_for_keys=not bool(self.password),
        )
        if self.key_path:
            connect_kwargs["key_filename"] = self.key_path
        if self.password:
            connect_kwargs["password"] = self.password
            connect_kwargs["look_for_keys"] = False

        try:
            client.connect(**connect_kwargs)
        except paramiko.BadHostKeyException as e:
            # 已知主机但密钥变了 —— 可能是 MITM, 强告警后由用户定夺
            fp = _ssh_fingerprint(e.key)
            accept = False
            if self.host_key_verifier is not None:
                try:
                    accept = bool(self.host_key_verifier(
                        e.hostname, e.key.get_name(), fp, True))
                except Exception:
                    accept = False
            if not accept:
                raise
            # 用户明确接受变更: 覆盖旧密钥后重连
            try:
                hk = client.get_host_keys()
                if e.hostname in hk:
                    del hk[e.hostname]
            except Exception:
                pass
            self._persist_host_key(client, e.hostname, e.key)
            client.connect(**connect_kwargs)

        chan = client.invoke_shell(term=self.term, width=cols, height=rows)
        chan.settimeout(0.0)  # 非阻塞
        self._client = client
        self._chan = chan
        self._alive = True

        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        import socket
        chan = self._chan
        while self._alive:
            try:
                if chan.recv_ready():
                    data = chan.recv(65536)
                    if not data:
                        self._emit_close("远端关闭连接")
                        break
                    self._emit_data(data)
                elif chan.closed or chan.exit_status_ready():
                    self._emit_close("会话已结束")
                    break
                else:
                    time.sleep(0.01)
            except socket.timeout:
                time.sleep(0.01)
            except Exception as e:
                self._emit_close(f"读取错误: {e}")
                break

    def write(self, data: bytes) -> None:
        if self._chan and self._alive:
            try:
                self._chan.send(data)
            except Exception as e:
                self._emit_close(f"写入错误: {e}")

    def resize(self, cols: int, rows: int) -> None:
        if self._chan and self._alive:
            try:
                self._chan.resize_pty(width=cols, height=rows)
            except Exception:
                pass

    def _do_close(self) -> None:
        if self._chan:
            self._chan.close()
        if self._client:
            self._client.close()


class SerialConnection(Connection):
    """基于 pyserial 的串口连接。"""

    def __init__(self, port: str, baudrate: int = 115200, bytesize: int = 8,
                 parity: str = "N", stopbits: float = 1) -> None:
        super().__init__()
        self.port = port
        self.baudrate = baudrate
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self._ser = None

    def start(self, cols: int, rows: int) -> None:
        import serial

        self._ser = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=self.bytesize,
            parity=self.parity,
            stopbits=self.stopbits,
            timeout=0.05,
        )
        self._alive = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        ser = self._ser
        while self._alive:
            try:
                n = ser.in_waiting
                data = ser.read(n or 1)
                if data:
                    self._emit_data(data)
            except Exception as e:
                self._emit_close(f"串口错误: {e}")
                break

    def write(self, data: bytes) -> None:
        if self._ser and self._alive:
            try:
                self._ser.write(data)
            except Exception as e:
                self._emit_close(f"串口写入错误: {e}")

    def _do_close(self) -> None:
        if self._ser:
            self._ser.close()


class LocalConnection(Connection):
    """本地 shell 进程 (Windows: cmd/powershell, *nix: bash)。

    Windows 下无真正的 PTY, 使用管道方式, 适合运行命令行程序。
    """

    def __init__(self, shell: str = "") -> None:
        super().__init__()
        import os
        self.shell = shell or (os.environ.get("COMSPEC", "cmd.exe")
                               if os.name == "nt" else "/bin/bash")
        self._proc = None
        self._use_winpty = False
        self._pty = None

    def start(self, cols: int, rows: int) -> None:
        import os
        if os.name == "nt":
            self._start_windows(cols, rows)
        else:
            self._start_posix(cols, rows)

    def _start_windows(self, cols: int, rows: int) -> None:
        # 优先尝试 pywinpty (提供真正的 ConPTY 终端)
        try:
            import winpty  # type: ignore
            self._pty = winpty.PtyProcess.spawn(self.shell, dimensions=(rows, cols))
            self._use_winpty = True
            self._alive = True
            self._reader = threading.Thread(target=self._read_loop_winpty, daemon=True)
            self._reader.start()
            return
        except Exception:
            pass
        # 回退: 普通管道子进程
        import subprocess
        self._proc = subprocess.Popen(
            self.shell,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            shell=False,
        )
        self._alive = True
        self._reader = threading.Thread(target=self._read_loop_pipe, daemon=True)
        self._reader.start()

    def _start_posix(self, cols: int, rows: int) -> None:
        import pty
        import os
        pid, fd = pty.fork()
        if pid == 0:
            os.execvp(self.shell, [self.shell])
        else:
            self._proc = pid
            self._pty_fd = fd
            self._alive = True
            self._set_winsize(cols, rows)
            self._reader = threading.Thread(target=self._read_loop_posix, daemon=True)
            self._reader.start()

    def _set_winsize(self, cols: int, rows: int) -> None:
        try:
            import fcntl, termios, struct
            fcntl.ioctl(self._pty_fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except Exception:
            pass

    def _read_loop_winpty(self) -> None:
        while self._alive:
            try:
                data = self._pty.read(65536)
                if not data:
                    self._emit_close("进程已退出")
                    break
                if isinstance(data, str):
                    data = data.encode("utf-8", "replace")
                self._emit_data(data)
            except EOFError:
                self._emit_close("进程已退出")
                break
            except Exception as e:
                self._emit_close(f"读取错误: {e}")
                break

    def _read_loop_pipe(self) -> None:
        while self._alive:
            try:
                data = self._proc.stdout.read(1)
                if not data:
                    self._emit_close("进程已退出")
                    break
                self._emit_data(data)
            except Exception as e:
                self._emit_close(f"读取错误: {e}")
                break

    def _read_loop_posix(self) -> None:
        import os
        while self._alive:
            try:
                data = os.read(self._pty_fd, 65536)
                if not data:
                    self._emit_close("进程已退出")
                    break
                self._emit_data(data)
            except OSError:
                self._emit_close("进程已退出")
                break
            except Exception as e:
                self._emit_close(f"读取错误: {e}")
                break

    def write(self, data: bytes) -> None:
        if not self._alive:
            return
        try:
            if self._use_winpty and self._pty:
                self._pty.write(data.decode("utf-8", "replace"))
            elif hasattr(self, "_pty_fd"):
                import os
                os.write(self._pty_fd, data)
            elif self._proc:
                self._proc.stdin.write(data)
                self._proc.stdin.flush()
        except Exception as e:
            self._emit_close(f"写入错误: {e}")

    def resize(self, cols: int, rows: int) -> None:
        if self._use_winpty and self._pty:
            try:
                self._pty.setwinsize(rows, cols)
            except Exception:
                pass
        elif hasattr(self, "_pty_fd"):
            self._set_winsize(cols, rows)

    def _do_close(self) -> None:
        import os
        try:
            if self._use_winpty and self._pty:
                self._pty.terminate(force=True)
            elif hasattr(self, "_pty_fd"):
                os.close(self._pty_fd)
            elif self._proc:
                self._proc.terminate()
        except Exception:
            pass
