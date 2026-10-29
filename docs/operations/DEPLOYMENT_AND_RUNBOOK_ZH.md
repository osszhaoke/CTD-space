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
   - 支持通过 `auto-bind` 在本机所在私网 `/24` 网段内受控只读探测 AirGradient 本地接口（`/measures/current`），亦支持现场负责人手工指定设备 IP；
   - 适配器仅执行单次 HTTP GET 请求，超时 5 秒，拒绝 HTTP 重定向，包体上限 64KB；
   - 适配器在接收载荷时即刻丢弃设备硬件序列号（`serialno`）、WiFi 信号（`wifi`）等硬件标识，仅保留载荷哈希；
   - 大屏展示服务默认严格绑定 `127.0.0.1` 环回地址并校验 `Host` 头防 DNS 重绑定；如需同网段 iPad / 电脑投屏浏览，可显式开启 `--lan` 局域网共享模式（仅对 RFC1918 私网开放只读浏览，`/admin` 字段修改接口仍严格锁定宿主机本机环回）；
   - 页面开启严苛 Content-Security-Policy，不加载任何外部脚本、样式或字体。
4. **Pod 6 接入与数据隐私边界（方案 B 授权落档）**：
   - Eight Sleep Pod 6 通过交互式账号认证在目标 Mac 本机只读接入（具备 macOS 系统根证书自动兜底能力），采集数据落入本机私密 SQLite；
   - **前端与导出隐私隔离**：大屏床微环境区采用 **5 卡镜像布局**（左侧床温与目标、右侧床温与目标、全夜床体温度、床周室温、温控运行状态），**前端界面与 AI 诊断导出均严格屏蔽心率、HRV、呼吸率等个人生理指标**；
   - 采集器仅执行严格只读 HTTP 请求拉取数据，严禁任何硬件温控反向写入或调节；
   - 运行时配置文件 `var/ctds-c01/config.json` 采用原子写入且权限严格限制为 `0600`；
   - 迁移交付包采用单一受限包（`var/ctds-c01/StarMap-C01-Mac-OneClick.zip`，权限 `0600`），包含快照数据以支持目标 Mac 无缝接续运行。

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
3. 执行 `self-test` 自动跑完 119 项单元/集成测试与大屏配置自检（确认房间名为“睡眠房”、双模块分区与默认黑夜模式就绪）；
4. 启动后台环回服务并自动打开 `http://127.0.0.1:8765/`（若需局域网内其他 iPad / Mac 只读访问大屏，可改为双击 `一键部署_StarMap_局域网共享.command` 或传入 `--lan`）。

### 2.1 检查运行环境
在终端中进入项目根目录并检查 Python 版本：
```bash
cd <repo-root>
python3 --version
# 期望：Python 3.10 或更高版本（macOS 自带 Python 3.9+ 亦可兼容运行）
```

### 2.2 自动发现绑定或初始化运行配置
在局域网内一键发现 AirGradient 并自动更新配置：
```bash
PYTHONPATH=development/ctds python3 -B -m room_display auto-bind
```
局域网探测发现 AirGradient 监测器后，`auto-bind` 会自动将 `source.mode` 从 `SYNTHETIC` 切换为 `LIVE` 并持久化写入配置。

若仅初始化配置或迁移已有配置：
- **初始化空配置模板**（若 `var/ctds-c01/config.json` 不存在则从 `config.example.json` 复制或写出合成默认模板，已存在不会覆盖）：
  ```bash
  PYTHONPATH=development/ctds python3 -B -m room_display init-config
  ```
- **配置结构迁移**（原子更新、归一化配置结构与单边 solo 模式，权限自动限制为 0600）：
  ```bash
  PYTHONPATH=development/ctds python3 -B -m room_display migrate-config
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
- **大屏展示**：[http://127.0.0.1:8765/](http://127.0.0.1:8765/)（默认首页 `/` 与 `/preview.html` 映射同一套现代驾驶舱大屏，支持右上角一键切换黑夜/白昼模式）
- **字段配置**：[http://127.0.0.1:8765/admin](http://127.0.0.1:8765/admin)

**大屏数据口径**（全部来自本机数据库的真实采样，不插值、不补点）：
- 小卡片趋势线：最近 1 小时，横轴按时间比例；采集中断超过约 3 分钟时曲线断开。服务刚启动时只有少量点，属正常。
- 点击卡片打开的大图：最近 24 小时（`/api/v1/rooms/<room>/series?metric=<指标>&hours=24`，最多 288 点）。
- 顶部时间轴：最近 24 个整点 + "实时"（`/api/v1/rooms/<room>/timeline?hours=24`）。每个整点取前后 30 分钟内最近的一次真实读数，没有则显示"—"；无读数的节点以虚线弱化。点任一整点回看，点"实时"返回。
- 床垫卡片只依据 Eight Sleep 自身状态：未绑定显示"Pod 6 · 待接入"与"—"，合成模式明确标注"合成测试数据"。
- 升级后端后需重启服务（重新运行一键部署或 `run`），新接口才会生效。

### 2.6 Eight Sleep Pod 6 床微环境指标与账号绑定（方案 B）
系统在 `development/ctds/room_display/eight_sleep.py` 内置 **Eight Sleep Pod 6** Level→℃ 查表转换（`RAW_TO_CELSIUS_MAP`，`-100..100` 映射 `13..44 ℃`）、`360s` 独立陈旧度窗口、采集器退避解耦与只读拉取逻辑。大屏床微环境区以 **5 卡对称镜像布局** 呈现空间与床体微气候（左右床温卡片纵轴刻度自动统一，便于直观对比）：
1. **左侧床温 & 左侧目标床温 (`pod_left_bed_temp` / `pod_left_target_temp`, `℃`)**
2. **右侧床温 & 右侧目标床温 (`pod_right_bed_temp` / `pod_right_target_temp`, `℃`)**
3. **全夜床体温度 (`pod_night_bed_temp`, `℃`)**：最近睡眠会话床温均值
4. **床周环境室温 (`pod_room_temp`, `℃`)**：床周环境感知温度
5. **左右温控运行状态与底座角度 (`pod_left_heating` / `pod_right_heating` 等)**

> **隐私隔离说明**：后端按方案 B 采集入本机私密 SQLite，但心率、HRV、呼吸率等个人生理指标**不在前端大屏展示，亦不包含在 AI 诊断导出文本中**。

**账号绑定与运行操作（本人裁决方案 B）**：
- **一键部署交互式引导**：`deploy_mac.sh` 在完成 AirGradient 自动发现后，若检测到 Eight Sleep 尚未处于 LIVE 模式，会自动提示是否立即绑定 Eight Sleep 账号（并自动探测修复 macOS `python.org` 根证书缺失问题）；
- **交互式独立绑定命令**（推荐，密码输入不回显，自动发现 user_id 与 device_id，权限 0600）：
  ```bash
  PYTHONPATH=development/ctds python3 -B -m room_display bind-eight-sleep
  ```
- **支持模式**：
  - `LIVE`：真实只读接入，自动维护 Bearer Token 与 Refresh Token，401 自动单次刷新，Token 失效转为 `AUTH_PAUSED`（AirGradient 暂停时亦不影响 Eight Sleep 独立按周期采集）；
  - `READY`：待接入占位状态，不发起网络请求；
  - `SYNTHETIC`：合成脱敏测试数据回放。
- **安全约束**：适配器绝不向 Eight Sleep 设备控制接口发起任何写请求；大屏导出至第三方 AI 分析时仅导出空间空气与床体温度切片（严格剔除个人生理指标、设备标识与网络凭据），由用户确认后再复制。

### 2.7 一键打包（开发机/母机操作）
在需要将工程打包迁移至目标机时，直接双击根目录下的 `一键打包_StarMap.command` 或在终端执行：
```bash
bash delivery/scripts/package_mac.sh
```
执行流程：
1. **环境与设备检查**：自动探测 Python 3.10+ 环境；若检测到本机尚未绑定 Eight Sleep，提示`是否先绑定 Eight Sleep 再打包？[y/N]`。选是可立即交互绑定，选否则生成未绑定包。
2. **全量沙箱测试**：自动在沙箱隔离环境（`CTDS_TEST_SANDBOX=1`）运行全量测试套件；若测试失败立即退出，退出码非零且不生成交付包。
3. **清单一致性核验**：执行 `verify_manifest.py`，确保所有文件身份及哈希与清单一致。
4. **单包构建与权限保护**：调用 `build_mac_bundle.py` 生成唯一交付包 `var/ctds-c01/StarMap-C01-Mac-OneClick.zip`，并自动设置权限为严格的 `0600`。
5. **打印完整摘要**：终端打印包路径、大小、SHA256、Eight Sleep 绑定状态、AirGradient 状态以及数据库各来源行数（仅统计数字，不泄露任何私密数值）。
*注：支持传入 `--no-prompt` 参数用于自动化流水线或无人值守打包；非终端环境（如 IDE 或后台任务）自动进入 `--no-prompt` 模式。*

### 2.8 安装时接入账密（目标机现场交互）
目标 Mac 解压交付包后，双击 `一键部署_StarMap.command`（内部执行 `deploy_mac.sh`）：
1. **环境自检与配置就绪**：探测 Python 3.10+ 环境，准备配置文件（权限 `0600`）。
2. **AirGradient 扫描与手动配置**：自动执行 `auto-bind` 扫描当前局域网内的 AirGradient。若未发现设备，将提示：
   ```
   未发现 AirGradient，可手动输入设备 IP（留空跳过）：
   ```
   输入合法 IP 后系统自动校验并持久化，下个周期自动连通。
3. **Eight Sleep 凭据探测与交互接入**：
   - **包内已含凭据**：脚本自动调用 `bind-eight-sleep --check` 静默验证连通性与设备发现。若验证通过，显示`Eight Sleep: 已接入 (LIVE)，验证成功`；若认证失败（如密码变更或 Token 失效），提示：
     ```
     Eight Sleep 认证失败，是否重新输入账号密码？[Y/n]
     ```
   - **包内无凭据**：直接友好提示：
     ```
     是否现在接入 Eight Sleep？[Y/n]
     ```
     默认为接入（直接回车）。
   - **输入安全与防呆**：邮箱使用 `input()` 输入，密码使用 `getpass` 隐藏输入不回显；输入错误支持最多重试 3 次；可直接输入空邮箱跳过，跳过后安全保持 `READY` 占位模式。
   - **首轮即刻校验**：绑定成功后立即拉取一次，终端展示 6 项 Pod 6 指标的提取状态（如 `OBSERVED`，绝不输出任何生理数值）。
4. **沙箱自检与服务拉起**：自动执行 `self-test` 确认大屏投影正常，随后拉起后台服务并打开浏览器访问大屏。
5. **部署终端汇总**：清晰打印 AirGradient 状态（在线/离线）、Eight Sleep 状态（在线/待接入/认证失效）及大屏访问地址。
*注：传入 `--no-prompt` 参数时全程不弹窗、不询问，仅尝试已有凭据，无凭据或认证失败时安全保持原状并在汇总中注明。*

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

### 3.5 官方守护进程管理与可选 launchd 配置

#### 官方推荐：后台常驻守护 (`deploy_mac.sh` / `daemon.pid`)
系统官方推荐的后台常驻管理方式为 `delivery/scripts/deploy_mac.sh` 内置的 `nohup` 守护机制：
1. **启动服务**：
   ```bash
   bash delivery/scripts/deploy_mac.sh
   ```
   脚本会自动检查旧实例，若已存在运行实例则通过读取 `var/ctds-c01/daemon.pid` 精确终止旧进程（绝不使用模糊误杀的 `pkill -f`），随后拉起新后台进程并将新 PID 写入 `var/ctds-c01/daemon.pid`。
2. **停止服务**：
   读取 PID 并核对命令行后精确停止：
   ```bash
   if [ -f var/ctds-c01/daemon.pid ]; then
     OLD_PID="$(cat var/ctds-c01/daemon.pid 2>/dev/null || true)"
     if [ -n "$OLD_PID" ] && ps -p "$OLD_PID" -o command= 2>/dev/null | grep -q "room_display"; then
       kill "$OLD_PID" 2>/dev/null || true
       echo "服务 (PID $OLD_PID) 已停止"
     else
       echo "未检测到活跃的 room_display 进程，跳过终止"
     fi
     rm -f var/ctds-c01/daemon.pid
   else
     echo "未检测到 daemon.pid，无运行实例"
   fi
   ```

#### 可选高级配置：系统级开机自启与保活 (launchd 模板)
若目标 Mac 展厅需要整机无人值守、登录自启与崩溃自动拉起，可选用随包提供的 `docs/operations/com.ctds.room-display.plist` 模板：
- 模板默认采用 `/usr/bin/env python3` 自动解析环境，且在 `EnvironmentVariables` 中显式设置 `PATH` 优先查找 `/opt/homebrew/bin` 与 `/usr/local/bin`，确保加载 3.10+ 版本，避免被 launchd 默认环境变量退化到系统旧版 Python。
- 部署与管理：
  ```bash
  # 1. 复制模板至 LaunchAgents
  cp docs/operations/com.ctds.room-display.plist ~/Library/LaunchAgents/
  # 2. 编辑 plist，将 WorkingDirectory 和日志路径替换为目标机实际解压目录绝对路径
  # 3. 加载并启动
  launchctl load ~/Library/LaunchAgents/com.ctds.room-display.plist
  # 4. 卸载停止
  launchctl unload ~/Library/LaunchAgents/com.ctds.room-display.plist
  ```
- **休眠与唤醒自愈**：
  - 休眠期间进程挂起，无无效网络重试；
  - 唤醒时本地时间跃升，超过 180 秒（`stale_after_s`）大屏自动标记为“已过期”，保留休眠前最后有效值；
  - 网络重新连通后采集线程自动自愈回最新读数，不发生未捕获异常。

### 3.6 Eight Sleep 认证失效处理与自愈
当 Eight Sleep 密码在官方 App 被修改，或长期断网导致 Refresh Token 过期时，系统会自动保护并自愈：
1. **系统表现**：
   - 采集线程在遇到 401 且无法通过 Refresh Token 或密码重刷时，自动将源状态标记为 `AUTH_PAUSED`，停止盲目网络重试；
   - 前端大屏床体状态标签显示“床垫认证失效 · 需重新绑定”，历史有效时序数据保持呈现并标注过期，不白屏；
   - 守护进程不崩溃退出，AirGradient 室内空气指标照常轮询更新。
2. **静默连通性诊断**：
   现场运维人员可使用 `--check` 命令快速诊断当前保存凭据是否仍有效：
   ```bash
   PYTHONPATH=development/ctds python3 -B -m room_display bind-eight-sleep --check
   ```
   输出 JSON 会明确返回 `{"authenticated": false, "device_ok": false, "outcome": "AUTH_REJECTED"}`，不修改任何现有配置。
3. **重新绑定与自愈**：
   在终端运行交互式绑定命令：
   ```bash
   PYTHONPATH=development/ctds python3 -B -m room_display bind-eight-sleep
   ```
   输入新密码后，客户端自动重新获取 Bearer Token 与设备 ID，更新 `0600` 权限配置，并触发一次即刻采集入库。后台轮询检测到新 Token 后状态立即自愈恢复，大屏徽章实时转为“Pod 6 · 实时在线”。

### 3.7 一键连通性与局域网共享诊断
现场遇到 Eight Sleep 云端连接异常或同网段其他设备无法访问大屏时，可直接双击根目录诊断器（报告自动脱敏并在桌面/访达高亮定位，不含密码、Token、完整邮箱、完整 UUID、MAC 地址或生理数值）：
1. **Eight Sleep 连通性与 TLS 证书诊断**：双击 `检测_EightSleep连通性.command`（或执行 `python3 -B delivery/scripts/diag_eight_sleep.py`），自动区分 `MISSING_LOCAL_CA`（本机 Python 缺根证书）与 `UNTRUSTED_CHAIN`、DNS/TCP/API 状态及脱敏日志摘要。
2. **局域网跨设备访问诊断**：双击 `检测_局域网共享连通性.command`（或执行 `python3 -B delivery/scripts/diag_lan_connectivity.py`），自动检查 `8765` 监听模式（`127.0.0.1` vs `0.0.0.0`）、物理网卡局域网 IP、Host 放行状态与 macOS 防火墙拦截提示。

---

## 4. 24 小时稳定性观察说明
按照工程纪律：
- 现场稳定性观察必须基于现场真实设备的连续运行记录进行统计；
- 观察时长由实际采集记录的起始时间与结束时间计算，不可使用测试加速或模型空闲时间替代；
- 在真实设备就位前，现场 24 小时观察记录登记为 `NOT_STARTED`。
