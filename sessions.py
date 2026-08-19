"""会话存储: 保存/加载连接配置到 JSON。

密码字段在磁盘上用数据密钥 AES-256-GCM 加密 (见 crypto.py)。
数据密钥本身由 Windows DPAPI 绑定当前用户保护, 存于 key.dat。
内存中的 Session.password 始终是明文, 供连接层直接使用;
落盘时透明加密, 读取时透明解密。无 key 时密码字段保持原样
(密文不解密), 保证不会崩溃或泄露。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from typing import List, Optional

from . import crypto

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".simple_crt")
SESSIONS_FILE = os.path.join(CONFIG_DIR, "sessions.json")
KEY_FILE = os.path.join(CONFIG_DIR, "key.dat")


@dataclass
class Session:
    name: str
    kind: str = "ssh"          # ssh | serial | local
    host: str = ""
    port: int = 22
    username: str = ""
    password: str = ""          # 内存中为明文; 磁盘上加密存储
    key_path: str = ""
    # serial
    serial_port: str = ""
    baudrate: int = 115200
    # local
    shell: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Session":
        known = {f: d[f] for f in Session.__dataclass_fields__ if f in d}
        return Session(**known)


class SessionStore:
    def __init__(self, path: str = SESSIONS_FILE,
                 key: Optional[bytes] = None,
                 key_path: str = KEY_FILE) -> None:
        self.path = path
        self.sessions: List[Session] = []
        # key 可显式注入 (测试); 否则自动 DPAPI 加载/生成
        if key is None:
            try:
                key = crypto.load_or_create_key(key_path)
            except Exception:
                key = None   # 密钥不可用时降级: 密码保持原样, 不崩溃
        self._key: Optional[bytes] = key
        self.load()
        # 首次运行 / 旧数据: 若磁盘存在明文密码则静默加密迁移
        if self._key is not None and self.has_plaintext_passwords():
            self.save()

    # ---- 磁盘 IO (透明加解密) ----
    def load(self) -> None:
        if not os.path.exists(self.path):
            self.sessions = []
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            sessions = []
            for x in data:
                s = Session.from_dict(x)
                # 磁盘密文 -> 内存明文 (需已解锁)
                if s.password and crypto.is_encrypted(s.password):
                    if self._key is not None:
                        try:
                            s.password = crypto.decrypt(self._key, s.password)
                        except Exception:
                            # 解密失败(密钥不对/损坏): 保留密文, 不崩溃
                            pass
                    # 未解锁: 保持密文, 连接时才需要
                sessions.append(s)
            self.sessions = sessions
        except Exception:
            self.sessions = []

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        out = []
        for s in self.sessions:
            d = s.to_dict()
            pw = d.get("password", "")
            # 内存明文 -> 磁盘密文 (需已解锁; 未解锁则原样写回避免二次加密)
            if pw and self._key is not None and not crypto.is_encrypted(pw):
                d["password"] = crypto.encrypt(self._key, pw)
            out.append(d)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)

    def has_plaintext_passwords(self) -> bool:
        """磁盘文件里是否存在未加密的明文密码 (用于迁移提示)。

        注意查的是磁盘而非内存: 内存中的 password 有意保持明文供连接层
        使用, 迁移的目标是磁盘不留明文。解锁后 save() 一次即完成迁移。
        """
        if not os.path.exists(self.path):
            return False
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return False
        return any(x.get("password") and not crypto.is_encrypted(x["password"])
                   for x in data)

    def add(self, session: Session) -> None:
        self.sessions.append(session)
        self.save()

    def update(self, index: int, session: Session) -> None:
        self.sessions[index] = session
        self.save()

    def remove(self, index: int) -> None:
        del self.sessions[index]
        self.save()
