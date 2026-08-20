# Simple CRT

一个轻量的 Windows 桌面 SSH 终端工具，界面与交互习惯参考 SecureCRT，用 PySide6 编写。适合需要管理多台主机会话、又不想装重型商业软件的场景。

> 本项目从需求分析、架构设计、编码实现、缺陷修复到构建发布，**完全由 GenericAgent (GA) 自主开发完成**。

## 功能特性

- **多会话管理**：以标签页形式管理多个 SSH 会话，支持主机、端口、用户名等配置，非默认端口准确保留。
- **会话密码加密存储**：密码在本地以密文保存，采用 Windows DPAPI + AES-256-GCM 双层保护（详见下文安全设计），无需主密码，开箱即用。
- **终端渲染**：基于 `pyte` 的终端仿真，`TERM=xterm-256color`。
  - 正确渲染中文/日韩全角字符（按双倍字符宽度绘制，不再出现"只显示一半"）。
  - 常显可拖动滚动条，滚轮按 `angleDelta` 累积滚动，支持高精度鼠标/触控板平滑滚动。
- **复制粘贴**：支持在终端中直接复制、粘贴文本。
- **配置隔离**：所有会话配置与密钥保存在用户目录 `~/.simple_crt/` 下，不污染代码目录。

## 安全设计

密码加密遵循 **Kerckhoffs 原则**（算法完全公开，安全性只来自 OS 用户凭据），即便攻击者拿到全部源码 + 所有文件也无法解密：

| 环节 | 方案 |
|------|------|
| 密钥生成 | 首次运行生成随机 256bit 密钥，存于 `key.dat` |
| 密钥保护 | Windows DPAPI (`CryptProtectData`) 将密钥绑定到"当前 Windows 用户账户"，换用户/换机器拿到 `key.dat` 也解不开 |
| 密码加密 | AES-256-GCM（认证加密，同时保密与防篡改） |
| 随机性 | 每条密码随机 12 字节 nonce |

- `sessions.json` 里的密码是 AES-256-GCM 密文（前缀 `enc:v1:`）。
- `key.dat` 里的密钥被 DPAPI 用你的 Windows 账户凭据加密（前缀 `dpapi:v1:`）。
- 攻击者没有你的 Windows 登录凭据就无法解出密钥，也就无法解密密码。
- 在同一台机器同一用户下运行则全程透明，无需任何输入。

## 直接使用（推荐给最终用户）

从 [Releases](https://github.com/JeanStory/simple-crt/releases) 下载 `SimpleCRT.exe`，双击即可运行，无需安装 Python 环境。

## 从源码运行（开发者）

需要 Python 3.9+ 与以下依赖：

```bash
pip install PySide6 pyte cryptography
```

启动（`run.py` 是顶层入口，已解决相对导入与连字符目录名问题）：

```bash
python run.py
```

> 说明：项目目录名 `simple-crt` 含连字符，不是合法的 Python 包名，无法用 `python -m` 直接运行。`run.py` 会把源码注册为合法包名 `app_src`，同时兼容开发态与打包（frozen）态，是最稳的启动方式。

## 构建二进制

使用 PyInstaller 打包为单文件 Windows 可执行程序：

```bash
pip install pyinstaller
pyinstaller --clean --noconfirm SimpleCRT.spec
```

产物位于 `dist/SimpleCRT.exe`（约 46 MB，onefile + windowed，无控制台窗口）。

`SimpleCRT.spec` 已随仓库提供，构建可复现。若手动指定参数，关键点：

- `--onefile --windowed`：单文件、无控制台。
- `--clean`：清除 `build/` 旧缓存（陈旧缓存是打出坏包的主要原因）。
- 显式声明隐藏依赖：`PySide6`、`pyte`、`cryptography`。

## 项目结构

```
simple-crt/
├── run.py            # 顶层入口（开发态 + frozen 态通用）
├── app.py            # 应用主入口 main()
├── mainwindow.py     # 主窗口、标签页管理
├── terminal.py       # 终端控件（pyte 仿真 + 渲染 + 滚动 + 复制粘贴）
├── connections.py    # SSH 连接后端
├── sessions.py       # 会话配置的读写
├── crypto.py         # 密码加密核心（DPAPI + AES-256-GCM）
├── dialogs.py        # 会话编辑等对话框
└── SimpleCRT.spec    # PyInstaller 构建配置
```

## 配置文件位置

- `~/.simple_crt/sessions.json` — 会话配置（密码字段为密文）
- `~/.simple_crt/key.dat` — DPAPI 保护的加密密钥

## 平台

- 目前面向 **Windows**（密码加密依赖 Windows DPAPI）。

## 开发说明

本项目的全部工作 —— 需求澄清、终端仿真实现、SSH 后端、密码加密方案设计、中文渲染与滚动条等缺陷修复、死代码清理、PyInstaller 打包与 GitHub Release 发布 —— 均由 **GenericAgent (GA)** 独立完成。
