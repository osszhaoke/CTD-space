# CTDS C01 · 现场部署与运维操作手册（单房中文大屏）

适用版本：CTDS-R03-C01  
交付目标日期：2026-10-19 12:00（Asia/Shanghai）现场交付  
运行环境：单台笔记本 + 外接显示器；macOS / Linux，Python 3.10+（纯标准库，零第三方依赖）

---

## 1. 架构与安全隔离边界

```
[ AirGradient LAN ]                     [ 本机回路 127.0.0.1 ]
  GET /measures/current                     
           │                                 ┌────────────────────────┐
           ▼                                 │ 浏览器大屏 (中性深色)  │
┌───────────────────────┐                    │ http://127.0.0.1:8765/ │
│ 采集适配器 (只读)     │                    └───────────▲────────────┘
│ (adapter / collector) │                                │
└──────────┬────────────┘                    ┌───────────┴────────────┐
           ▼                                 │ 环回服务 (server)      │
┌───────────────────────┐                    │ - GET /api/v1/...      │
│ SQLite WAL 持续存储   │───────────────────►│ - PATCH /admin/fields  │
│ var/ctds-c01/*.sqlite3│                    │   (仅限本机严格白名单) │
└───────────────────────┘                    └────────────────────────┘
```

1. **零第三方依赖**：系统完全基于 Python 标准库（`sqlite3`、`http.server`、`urllib`、`json`、`hashlib`）实现，不依赖 pip、npm、外部 CDN 或云端服务。
2. **私密配置与数据隔离**：
   - 现场设备 IP 与私密配置存放在 `var/ctds-c01/config.json`；
   - 运行时数据库存放在 `var/ctds-c01/ctds_c01.sqlite3`；
   - `var/` 目录已被 `.gitignore` 严格忽略，绝不进 Git、不进交付 ZIP、不进入模型上下文。
3. **网络与设备安全**：
   - 严禁局域网扫描、严禁猜测设备 IP；设备地址必须由现场网络负责人确认并受控填入；
   - 适配器仅执行单次 HTTP GET 请求，超时 5 秒，拒绝 HTTP 重定向，包体上限 64KB；
   - 适配器在接收载荷时即刻丢弃设备硬件序列号（`serialno`）、WiFi 信号（`wifi`）等硬件标识，仅保留载荷哈希；
   - 环回展示服务严格绑定 `127.0.0.1`，校验 Host 头，防 DNS 重绑定；
   - 页面开启严苛 Content-Security-Policy，不加载任何外部脚本、样式或字体。
4. **Pod 5 边界**：
   - Eight Sleep Pod 5 仅作为 `READY_NOT_CONNECTED` 状态占位卡展示；
   - 严禁配置账号登录、严禁读取真人生理数据、严禁控制温控硬件。

---

## 2. 现场部署与快速启动（支持另一台 Mac 一键部署）

### 2.0 另一台 Mac 一键部署包（推荐）
将生成的一键部署包 `var/ctds-c01/StarMap-C01-Mac-OneClick.zip` 拷贝至目标 Mac 并解压后，直接双击 `一键部署_StarMap.command` 或在终端执行：
```bash
bash delivery/scripts/deploy_mac.sh
```
脚本将自动完成以下 4 步：
1. 检查 Python 3.10+ 环境并载入离线基准库（若首次运行）；
2. 执行 `auto-bind` 自动扫描当前网段内的 AirGradient 监测器，发现后自动更新 `var/ctds-c01/config.json` 并拉取最新读数（若运行时设备重连或换 IP，后台采集器亦会自动重发现并更新绑定）；
3. 执行 `self-test` 自动跑完 60 项单元/集成测试与大屏配置自检（确认房间名为“睡眠房”、双模块分区与默认黑夜模式就绪）；
4. 启动后台环回服务并自动打开 `http://127.0.0.1:8765/`。

### 2.1 检查运行环境
在终端中进入项目根目录并检查 Python 版本：
```bash
cd /Users/osszhaoke/Downloads/001-CTDSvFoundation-StarMap
python3 --version
# 期望：Python 3.10 或更高版本
```

### 2.2 自动发现绑定或初始化运行配置
在局域网内一键发现 AirGradient 并自动更新配置：
```bash
PYTHONPATH=development/ctds python3 -B -m room_display auto-bind
```
若仅初始化合成模板（如果 `var/ctds-c01/config.json` 不存在会自动生成合成模板，已存在不会覆盖）：
```bash
PYTHONPATH=development/ctds python3 -B -m room_display init-config
```

### 2.3 手工或自动绑定现场真实 AirGradient 监测器
配置文件 `var/ctds-c01/config.json` 结构如下（`auto-bind` 会自动识别网段内设备并填入 `base_url`）：
```json
{
  "room_id": "demo-room-01",
  "room_name_zh": "睡眠房",
  "db_path": "var/ctds-c01/ctds_c01.sqlite3",
  "listen_port": 8765,
  "poll_interval_s": 60,
  "timeout_s": 5,
  "retries": 2,
  "max_backoff_s": 300,
  "stale_after_s": 180,
  "source": {
    "source_id": "ag-room-01",
    "mode": "LIVE",
    "base_url": "http://192.0.2.10",
    "binding_revision": 1
  }
}
```
*注：`base_url` 为现场合法 IP（格式为 `http://ip` 或 `http://host:port`，不带多余路径）。*

### 2.4 单次采集诊断与全量自动测试
执行单次采集测试连通性：
```bash
PYTHONPATH=development/ctds python3 -B -m room_display collect-once
```
执行全量自动化测试与大屏自检：
```bash
PYTHONPATH=development/ctds python3 -B -m room_display self-test
```

### 2.5 启动大屏与采集服务
执行一键启动命令：
```bash
PYTHONPATH=development/ctds python3 -B -m room_display run
```
终端将输出：
```
display: http://127.0.0.1:8765/  admin: /admin  mode=LIVE
```
打开本地浏览器访问：
- **大屏展示**：[http://127.0.0.1:8765/](http://127.0.0.1:8765/)（默认黑夜模式，支持右上角一键切换白昼模式）
- **字段配置**：[http://127.0.0.1:8765/admin](http://127.0.0.1:8765/admin)

### 2.6 Eight Sleep Pod 5 六项睡眠指标与云端只读 API 接入（供后续 Coding Agent / 目标 Mac 交接）
系统已在 `development/ctds/room_display/eight_sleep.py` 内置完整的 **Eight Sleep Pod 5 六项睡眠与温控指标** 提取、Level→℃ 查表转换（`RAW_TO_CELSIUS_MAP`，`-100..100` 映射 `13..44 ℃`）以及只读云端客户端：
1. **心率 (`pod_heart_rate`, `bpm`)**：`GET /v1/users/{user_id}/trends` → `days[-1].sessions[-1].timeseries.heartRate[-1][1]`
2. **心率变异性 HRV (`pod_hrv`, `ms`)**：`GET /v1/users/{user_id}/trends` → `days[-1].sleepQualityScore.hrv.current`
3. **呼吸率 (`pod_respiratory_rate`, `次/分`)**：`GET /v1/users/{user_id}/trends` → `days[-1].sleepQualityScore.respiratoryRate.current`
4. **床温 (`pod_bed_temp`, `℃`)**：`GET /v1/users/{user_id}/temperature` → `currentDeviceLevel` 经 `RAW_TO_CELSIUS_MAP` 查表转 `℃`
5. **目标床温 (`pod_target_bed_temp`, `℃`)**：`GET /v1/devices/{device_id}` → `leftTargetHeatingLevel` / `rightTargetHeatingLevel` 经 `RAW_TO_CELSIUS_MAP` 查表转 `℃`
6. **室温 (`pod_room_temp`, `℃`)**：`GET /v1/users/{user_id}/trends` → `days[-1].sessions[-1].timeseries.tempRoomC[-1][1]`

当后续 Coding Agent 或目标 Mac 拿到 Eight Sleep 账号后，可通过以下任一方式一键完成鉴权、用户/设备 ID 自动发现与 LIVE 采集：
- **方式 A（CLI 一键绑定并测试首轮拉取）**：
  ```bash
  PYTHONPATH=development/ctds python3 -B -m room_display --config var/ctds-c01/config.json \
    --email "YOUR_EMAIL" --password "YOUR_PASSWORD" --side auto --es-mode LIVE bind-eight-sleep
  ```
- **方式 B（环境变量或部署脚本参数直连）**：
  ```bash
  EIGHT_SLEEP_EMAIL="YOUR_EMAIL" EIGHT_SLEEP_PASSWORD="YOUR_PASSWORD" bash delivery/scripts/deploy_mac.sh
  ```

---

## 3. 运维操作与常见状态处理

### 3.1 状态诊断命令
查看当前存储记录总数及最新一次采集状态：
```bash
PYTHONPATH=development/ctds python3 -B -m room_display status
```

### 3.2 故障现象与系统自愈表现
| 故障场景 | 大屏表现 | 系统行为与恢复方法 |
|---|---|---|
| **设备网络波动/断线** | 顶部状态变为“无法连接”，错误横幅提示“无法连接设备”；数值保持最后一次有效值并标为“已过期”，不白屏 | 采集器自动执行 2 秒/4 秒有界重试；若持续失败则退避至最高 300 秒，网络恢复后下一轮轮询自动自愈 |
| **设备响应超时** | 错误横幅提示“设备响应超时”，大屏保持旧值 | 超时上限为 5 秒，不卡死采集线程；下一周期继续采集 |
| **认证拒绝 (401/403)** | 状态变为“已暂停（认证拒绝）” | 系统将源状态标记为 `AUTH_PAUSED`，停止无限盲目重试；需现场人员确认设备权限后重启服务 |
| **无效数据/部分字段为空** | 对应指标卡片显示“设备返回空值”或“未提供”，其余指标正常显示 | 严格区分 0（合法读数）、null（空值）与缺字段，系统不崩溃 |

### 3.3 本机大屏字段调整
1. 访问 `http://127.0.0.1:8765/admin`；
2. 勾选需要在大屏展示的指标（支持 PM2.5、CO2、温度、湿度、PM1.0、PM10、TVOC、NOx 等）；
3. 可修改指标顺序（1–99）与中文名称（如改为“主卧空气质量”）；
4. 点击“保存配置”，大屏在下一次轮询（5秒内）自动按新顺序与新名称呈现；
5. 配置存入 SQLite 的 `display_settings` 表并自增版本号，重启后永久保留。

### 3.4 进程退出与同库重新打开（冷启动持久化读回）
- **边界说明**：已有自动化测试（`test_store.py` 和 `test_fault_recovery.py`）实际证明了**进程正常退出或被异常终止后，重新打开同一 SQLite 数据库文件时历史记录与管理员展示配置完整读回**的能力。系统未进行整机物理掉电测试，现场运行建议依赖稳定的笔记本供电或电池保护。
- **优雅停机**：在终端按 `Ctrl+C` 或发送 `SIGTERM` 信号。服务会停止采集线程、关闭 HTTP 监听并安全提交 SQLite 事务。
- **重新拉起验证**：
  直接重新执行 `PYTHONPATH=development/ctds python3 -B -m room_display run`。
  服务重新打开数据库，大屏立即呈现已有历史时序与最新配置。

### 3.5 系统级开机自启、崩溃保活与 Mac 休眠唤醒 (launchd)
现场展示笔记本如果需要长期无人值守运行，可使用轻量 macOS `launchd` 用户代理配置（模版见 `docs/operations/com.ctds.room-display.plist`）：

1. **部署配置到 LaunchAgents**：
   ```bash
   cp docs/operations/com.ctds.room-display.plist ~/Library/LaunchAgents/
   # 检查 plist 中的 python3 路径与 WorkingDirectory 是否与本机一致
   ```
2. **加载并启动服务**：
   ```bash
   launchctl load ~/Library/LaunchAgents/com.ctds.room-display.plist
   ```
3. **卸载/停止服务**：
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.ctds.room-display.plist
   ```
4. **系统异常与休眠唤醒行为机制**：
   - **进程意外崩溃 (Crash)**：配置了 `KeepAlive: { SuccessfulExit: false }`，当进程异常退出码非 0 时，macOS 会在 `ThrottleInterval`（10秒）后自动重启拉起服务。
   - **系统开机登录**：配置了 `RunAtLoad: true`，开机登录进入桌面后自动启动大屏后台服务。
   - **Mac 笔记本休眠与合盖唤醒**：
     - 休眠期间进程挂起，不会产生堆积网络请求；
     - 唤醒时本地时间跃升，若休眠超过 180 秒（`stale_after_s`），大屏自动标记为“已过期”，保留休眠前最后有效值；
     - 网络重新连通后，采集线程在下一周期自动拉取新读数，大屏无缝自愈回“实时数据”；
     - 若唤醒瞬间网络未通，采集器捕获 `NO_CONNECTION` 并退避重试，不发生未处理崩溃。

---

## 4. 24 小时稳定性观察说明
按照工程纪律：
- 现场稳定性观察必须基于现场真实设备的连续运行记录进行统计；
- 观察时长由实际采集记录的起始时间与结束时间计算，不可使用测试加速或模型空闲时间替代；
- 在真实设备就位前，现场 24 小时观察记录登记为 `NOT_STARTED`。
