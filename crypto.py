"""密码加密核心 (Kerckhoffs 原则: 算法全公开, 安全只来自 OS 用户凭据)。

设计 (无主密码, 零输入):
  - 密钥:   首次运行生成随机 256bit 密钥, 用 Windows DPAPI 保护后存 key.dat
  - DPAPI:  CryptProtectData 把密钥绑定到"当前 Windows 用户账户"。
            换个用户/换台机器拿到 key.dat 也解不开 (缺该用户的登录凭据)。
  - 加密:   AES-256-GCM (认证加密, 同时保密与防篡改)
  - 每条密码随机 12 字节 nonce

即便攻击者拿到全部源码 + 所有文件 (sessions.json + key.dat):
  - sessions.json 里的密码是 AES-256-GCM 密文
  - key.dat 里的密钥被 DPAPI 用"你的 Windows 账户凭据"加密
攻击者没有你的 Windows 登录凭据就无法解出密钥, 也就无法解密密码。
在同一台机器同一个用户下运行则全程透明, 无需任何输入。
"""
from __future__ import annotations

import base64
import os
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ---- 密文前缀标记, 用于区分明文/密文 (向后兼容旧明文密码) ----
ENC_PREFIX = "enc:v1:"

KEY_LEN = 32          # AES-256
NONCE_LEN = 12        # GCM 推荐 96bit

# DPAPI 保护密钥文件里的前缀, 标识这是 DPAPI 保护过的 blob
DPAPI_PREFIX = "dpapi:v1:"


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(txt: str) -> bytes:
    return base64.b64decode(txt.encode("ascii"))


# ==================== DPAPI (Windows 用户绑定) ====================

def _dpapi_available() -> bool:
    return os.name == "nt"


def dpapi_protect(data: bytes) -> bytes:
    """用 DPAPI 把 data 绑定到当前 Windows 用户加密。非 Windows 抛 RuntimeError。"""
    if not _dpapi_available():
        raise RuntimeError("DPAPI 仅在 Windows 可用")
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _to_blob(b: bytes) -> DATA_BLOB:
        buf = ctypes.create_string_buffer(b, len(b))
        return DATA_BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    in_blob = _to_blob(data)
    out_blob = DATA_BLOB()
    # CRYPTPROTECT_UF_LOCAL_MACHINE 不设 => 绑定"当前用户"(最强隔离)
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(in_blob), None, None, None, None, 0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise RuntimeError("CryptProtectData 失败")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def dpapi_unprotect(data: bytes) -> bytes:
    """用 DPAPI 解出 data。凭据不符 (换用户/换机器) 会抛 RuntimeError。"""
    if not _dpapi_available():
        raise RuntimeError("DPAPI 仅在 Windows 可用")
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _to_blob(b: bytes) -> DATA_BLOB:
        buf = ctypes.create_string_buffer(b, len(b))
        return DATA_BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    in_blob = _to_blob(data)
    out_blob = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise RuntimeError("CryptUnprotectData 失败 (凭据不符或数据损坏)")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


# ==================== 密钥管理 (自动, 无主密码) ====================

def load_or_create_key(key_path: str) -> bytes:
    """加载(或首次生成) 32 字节数据密钥。

    - 已存在 key.dat: 用 DPAPI 解出返回。
    - 不存在: 随机生成 32 字节, DPAPI 保护后写入 key.dat, 返回明文密钥。

    非 Windows 平台降级: 密钥明文存 key.dat (仍靠文件系统权限保护)。
    """
    if os.path.exists(key_path):
        with open(key_path, "r", encoding="utf-8") as f:
            stored = f.read().strip()
        if stored.startswith(DPAPI_PREFIX):
            blob = _b64d(stored[len(DPAPI_PREFIX):])
            return dpapi_unprotect(blob)
        # 非 DPAPI (降级明文密钥)
        return _b64d(stored)

    key = os.urandom(KEY_LEN)
    os.makedirs(os.path.dirname(key_path), exist_ok=True)
    if _dpapi_available():
        protected = dpapi_protect(key)
        content = DPAPI_PREFIX + _b64e(protected)
    else:
        content = _b64e(key)
    # 原子写 + 收紧权限
    tmp = key_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, key_path)
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return key


# ==================== 单条密码加解密 (AES-256-GCM) ====================

def encrypt(key: bytes, plaintext: str) -> str:
    """加密单条密码 -> 'enc:v1:<base64(nonce+ciphertext)>'。空串原样返回。"""
    if plaintext == "":
        return ""
    aes = AESGCM(key)
    nonce = os.urandom(NONCE_LEN)
    ct = aes.encrypt(nonce, plaintext.encode("utf-8"), None)
    return ENC_PREFIX + _b64e(nonce + ct)


def decrypt(key: bytes, token: str) -> str:
    """解密 'enc:v1:...' 密文。非密文(旧明文)原样返回, 保证向后兼容。"""
    if not token.startswith(ENC_PREFIX):
        return token  # 旧的明文密码
    blob = _b64d(token[len(ENC_PREFIX):])
    nonce, ct = blob[:NONCE_LEN], blob[NONCE_LEN:]
    pt = AESGCM(key).decrypt(nonce, ct, None)  # 篡改/错key会抛异常
    return pt.decode("utf-8")


def is_encrypted(token: str) -> bool:
    return token.startswith(ENC_PREFIX)
