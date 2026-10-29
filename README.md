# CTD-space (CTDS) · 空间与微环境授权上下文数据底座 (`sqlite-lite` 单机轻量版)

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.9+-3776AB?logo=python&logoColor=white)](development/ctds/room_display/)
[![SQLite](https://img.shields.io/badge/SQLite-WAL_Append_Only-003B57?logo=sqlite&logoColor=white)](development/ctds/room_display/store.py)

**CTD-space (CTDS)** 是与 **PTDS** 协同演进的**空间与微环境授权上下文数据底座（Data Infra）**。

本分支（`release/v0.1-sqlite-lite`）为 **零第三方依赖（纯 Python 标准库 + SQLite WAL + 原生 HTML/CSS/JS）的单机极简大屏维护线**，适用于无需安装 Node.js / PostgreSQL、开箱双击即可在 macOS 笔记本或轻量容器中运行的展厅与单房间微环境监测场景（多容器 Next.js + PostgreSQL 栈请参见 `main` 分支）。

---

## 核心特性（StarMap C01 · `v0.1.1-sqlite-lite`）

- **双模块空间与床体微环境驾驶舱（4SEAS 极奢看板）**：
  - **室内环境 · 空间空气与温湿（AirGradient）**：只读采集 PM2.5、CO₂、温度、相对湿度、TVOC 指数、NOx 指数、PM1.0、PM10 等实时读数；支持同网段 `/24` 自动发现与换 IP 自愈重绑；内置 1 小时卡片趋势线、点击展开 24 小时大图及顶部 24 整点时间轴回溯（真实采样不插值）。
  - **床微环境 · 双侧对称温控看板（Eight Sleep Pod 6）**：呈现左右两侧当前床温与目标床温（纵轴刻度自动对齐，直观对比双侧温差）、全夜床温均值、床周环境室温与温控运行状态；内置 `RAW_TO_CELSIUS_MAP`（`-100..100` → `13..44 ℃`）查表转换、`360s` 独立陈旧度窗口及 AirGradient 退避/暂停解耦。
  - **严格隐私与只读安全边界**：所有外部设备请求严格只读，绝不发起硬件反向控制；大屏展示与 AI 微环境切片导出**严格剔除心率、HRV、呼吸率等个人生理指标**及一切设备序列号、网络凭据。
- **双模式视觉、局域网只读投屏与 macOS TLS 自愈**：
  - 内置 **深翠黑夜模式**（默认）与 **暖白白昼模式** 一键切换，开启严格 `Content-Security-Policy`（零外部 CDN、零内联脚本）。
  - 默认严格绑定 `127.0.0.1:8765` 并校验 `Host` 防 DNS 重绑定；支持通过 `--lan`（或双击 `一键部署_StarMap_局域网共享.command`）向同网段 iPad / 电脑开放**只读大屏共享**（`/admin` 字段配置写接口仍严格锁定宿主机本机环回）。
  - 自动兼容修复 macOS `python.org` 安装版缺失根证书问题（运行时 `certifi` + `/etc/ssl/cert.pem` 系统证书双层严格校验兜底，始终保持 `CERT_REQUIRED`）。

---

## 快速启动与双击工具箱（macOS）

仓库根目录提供 5 个免终端双击入口：

| 双击脚本 | 用途 | 对应命令行 |
|---|---|---|
| `一键部署_StarMap.command` | 本机环回部署（自动发现 AirGradient、引导绑定 Eight Sleep、119 项自检并拉起 `127.0.0.1:8765`） | `bash delivery/scripts/deploy_mac.sh` |
| `一键部署_StarMap_局域网共享.command` | 开启同网段局域网只读投屏共享（供同一 Wi-Fi 下 iPad / 其他电脑浏览大屏） | `bash delivery/scripts/deploy_mac.sh --lan` |
| `一键打包_StarMap.command` | 运行沙箱测试并生成单机迁移包 `var/ctds-c01/StarMap-C01-Mac-OneClick.zip`（权限 `0600`） | `bash delivery/scripts/package_mac.sh` |
| `检测_EightSleep连通性.command` | 诊断 DNS / TCP / TLS 根证书链与 API 状态，在桌面生成脱敏报告 | `python3 -B delivery/scripts/diag_eight_sleep.py` |
| `检测_局域网共享连通性.command` | 诊断 `8765` 监听模式、物理网卡 IP、Host 放行与 macOS 防火墙状态 | `python3 -B delivery/scripts/diag_lan_connectivity.py` |

---

## 交互式安全接入 Eight Sleep

为避免命令历史泄露密码，系统采用交互式隐藏输入（`getpass`）完成认证，并将 `var/ctds-c01/config.json` 原子设为 `0600` 权限：

```bash
# 交互式绑定 Eight Sleep（密码输入不回显，自动发现 user_id 与 device_id）
PYTHONPATH=development/ctds python3 -B -m room_display bind-eight-sleep

# 静默检查当前已存凭据与连通性（不修改配置文件）
PYTHONPATH=development/ctds python3 -B -m room_display bind-eight-sleep --check
```

> 所有运行期配置、凭据、日志与 SQLite 数据库仅保存在本地 `var/ctds-c01/`（已列入 `.gitignore`），绝不进入版本库。

---

## 轻量容器化启动（可选 Docker Compose）

```bash
docker compose up -d --build
# 默认仅映射宿主机环回 127.0.0.1:8765:8765，持久化挂载 ./var:/app/var
```

---

## 开发与自动化验证命令

```bash
# 运行全量沙箱测试套件（119 项单元/契约/故障自愈测试，严格零警告模式）
CTDS_TEST_SANDBOX=1 PYTHONPATH=development/ctds python3 -B -W error \
  -m unittest discover -s development/ctds/tests/room_display -p "test_*.py"

# 运行 CLI 内置全链路自检（自动校验真实运行库零篡改 db_unmutated: true）
PYTHONPATH=development/ctds python3 -B -m room_display self-test
```

详细现场部署与排障手册请参阅 [`docs/operations/DEPLOYMENT_AND_RUNBOOK_ZH.md`](docs/operations/DEPLOYMENT_AND_RUNBOOK_ZH.md)。

---

## License

Licensed under the **Apache License, Version 2.0** ([`LICENSE`](LICENSE)).
