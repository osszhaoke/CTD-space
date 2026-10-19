# CTD-space (CTDS) · 空间与微环境授权上下文数据底座

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

**CTD-space (CTDS)** 是与 **PTDS** 协同演进的**空间与微环境授权上下文数据底座（Data Infra）**。

它致力于在保护个人数据主权的前提下，构建人身状态与物理微环境之间的授权上下文桥梁——将**室内空间环境**（温度、湿度、CO₂、PM2.5、TVOC/NOx 等挥发性物质）与**睡眠微环境及人体体征**（床温调节、心率、HRV、呼吸率、睡眠阶段等）置于统一的时空上下文中进行协同感知与分析。

- **开源协议（License）**：`Apache-2.0`
- **当前已接入设备**：**AirGradient**（室内空气与温湿监测）、**Eight Sleep**（睡眠体征与床微环境温控）
- **Roadmap（与 PTDS 互联）**：下一步将支持与本地加密版 **PTDS** 端到端互联，未来将承载受控域内的**多方安全计算（MPC）**、**匿踪聚合分析**，以及**细粒度、可随时撤回的匿名上下文授权**。
- **线下体验**：本项目将在 **北京 ACC「四海健康微环境体验工坊」** 现场展出（届时也会同步带来全新本地加密版 PTDS），欢迎感兴趣的小伙伴来现场体验交流！

---

## 核心特性（StarMap C01）

面向睡眠与微环境场景的零外部依赖（Python 3.10+ 标准库 + SQLite WAL）单页空间环境与床微环境看板：

- **双模块空间与体征架构**：
  - **室内环境 · 空间空气与温湿**：7 项空气与温湿指标（温度、相对湿度、CO₂、PM2.5、TVOC 指数、NOx 指数、PM0.3 颗粒），支持局域网自动发现 AirGradient 监测设备并自助重绑，具备 11:50–12:00 历史时间轴小点悬停/点击交互。
  - **床微环境 · 睡眠体征与温控**：6 项睡眠与温控指标（睡眠得分、睡眠阶段、心率、HRV、呼吸率、床温调节），集成 Eight Sleep 云端只读接入能力，支持凭证就绪后一键切换 `LIVE` 同步，未绑定时自动展示演示基线读数。
- **双模式毛玻璃视觉与 iPad Pro 投屏适配**：
  - 内置 **深翠黑夜模式**（默认）与 **暖白白昼模式** 一键切换，搭载 `backdrop-filter` 多层背景毛玻璃与环境光晕。
  - 针对 **iPad Pro 11" / 12.9"（横屏 & 竖屏）** 及桌面大屏做响应式自适应布局与触控热区优化，支持 `Add to Home Screen` 全屏 Web App 模式。

---

## 一键部署与自检（macOS）

双击仓库根目录的 **`一键部署_StarMap.command`**，或在终端执行：

```bash
bash delivery/scripts/deploy_mac.sh
```

脚本会自动完成：
1. 检测本机 Python 3.10+ 环境；
2. 局域网并发探测室内环境监测设备并更新 `var/ctds-c01/config.json`（若未发现在线设备，自动保持离线冻结回放）；
3. 运行全量 60 项单元与 UI 自检测试（`self-test`）；
4. 启动后台环回守护服务并打开 `http://127.0.0.1:8765/`。

---

## 床微环境（Eight Sleep）接入命令

获取到账号或 Bearer Token 后，可通过以下命令完成绑定与首次同步：

```bash
# 方式 A：通过一键部署脚本直接传入凭据
bash delivery/scripts/deploy_mac.sh --es-email "your@email.com" --es-password "your_password" --es-side auto

# 方式 B：单独运行 CLI 绑定指令
PYTHONPATH=development/ctds python3 -B -m room_display --config var/ctds-c01/config.json \
  --email "your@email.com" --password "your_password" --side auto --es-mode LIVE bind-eight-sleep
```

> 所有运行期配置、凭据与 SQLite 数据库仅保存在本地 `var/ctds-c01/`（已列入 `.gitignore`），不会进入版本库。

---

## 常用开发与验证命令

```bash
# 运行全量自动化测试套件 (60 tests)
PYTHONPATH=development/ctds python3 -B -m unittest discover -s development/ctds/tests/room_display -v

# 运行 CLI 内置自检
PYTHONPATH=development/ctds python3 -B -m room_display --config var/ctds-c01/config.json self-test

# 生成可分发给其他 Mac 的脱敏一键部署包 (输出至 var/ctds-c01/StarMap-C01-Mac-OneClick.zip)
python3 delivery/scripts/build_mac_bundle.py
```

详细运维说明请参阅 [`docs/operations/DEPLOYMENT_AND_RUNBOOK_ZH.md`](docs/operations/DEPLOYMENT_AND_RUNBOOK_ZH.md)。

---

## License

Licensed under the **Apache License, Version 2.0**.
