# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

# connections.py 里 paramiko/serial/winpty 都是函数内延迟 import,
# PyInstaller 静态分析扫不到, 必须显式 collect_all 把二进制/数据/子模块全收进来。
# paramiko 还依赖 bcrypt / pynacl 的 C 扩展。
_extra_bins, _extra_datas, _extra_hidden = [], [], []
for _pkg in ('paramiko', 'bcrypt', 'nacl', 'serial', 'winpty'):
    try:
        _b, _d, _h = collect_all(_pkg)
        _extra_bins += _b
        _extra_datas += _d
        _extra_hidden += _h
    except Exception:
        pass  # 该环境未装(如winpty可能缺)则跳过, 不阻断构建


a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=_extra_bins,
    datas=[('app.py', 'app_src'), ('connections.py', 'app_src'), ('crypto.py', 'app_src'), ('dialogs.py', 'app_src'), ('mainwindow.py', 'app_src'), ('sessions.py', 'app_src'), ('terminal.py', 'app_src'), ('zmodem.py', 'app_src'), ('zmodem_ui.py', 'app_src'), ('__init__.py', 'app_src')] + _extra_datas,
    hiddenimports=_extra_hidden + ['pyte', 'cryptography',
        'cryptography.hazmat.backends',
        'cryptography.hazmat.backends.openssl',
        'cryptography.hazmat.bindings',
        'cryptography.hazmat.bindings._rust',
        'cryptography.hazmat.bindings.openssl',
        'cryptography.hazmat.primitives.ciphers',
        'cryptography.hazmat.primitives.ciphers.aead',
        'cryptography.hazmat.primitives.ciphers.algorithms',
        'cryptography.hazmat.primitives.ciphers.modes',
        'cryptography.hazmat.primitives.kdf',
        'cryptography.hazmat.primitives.kdf.scrypt',
        'cryptography.hazmat.primitives.kdf.pbkdf2',
        'cryptography.hazmat.primitives.hashes',
        'cryptography.hazmat.primitives.padding'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='SimpleCRT',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
