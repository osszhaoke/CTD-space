# CTD-space · 睡眠房单页环境与床微环境大屏 (StarMap C01)

面向睡眠房场景的零外部依赖（Python 3.10+ 标准库 + SQLite WAL）单页空间环境与床微环境看板：

- **双模块去品牌化架构**：
  - **室内环境 · 空间空气与温湿**：7 项空气与温湿指标（温度、相对湿度、CO₂、PM2.5、TVOC 指数、NOx 指数、PM0.3 颗粒），支持局域网自动发现监测设备并自助重绑，具备 11:50–12:00 历史时间轴小点悬停/点击交互。
  - **床微环境 · 睡眠体征与温控**：6 项睡眠与温控指标（睡眠得分、睡眠阶段、心率、HRV、呼吸率、床温调节），支持凭证就绪后一键切换 `LIVE` 云端只读同步，未绑定时自动展示演示基线读数。
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
