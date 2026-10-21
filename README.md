# CTD-space (CTDS) · 空间与微环境授权上下文数据底座

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](compose.yaml)
[![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)](collector/)
[![Next.js](https://img.shields.io/badge/Next.js-15-black?logo=next.js&logoColor=white)](web/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-17-4169E1?logo=postgresql&logoColor=white)](db/)

**CTD-space (CTDS)** 是面向空间与睡眠微环境的**授权上下文数据底座（Data Infra）**，致力于在保护个人数据主权的前提下，构建人身状态与物理微环境之间的桥梁。

> **注：** 若您在无 Docker 的极简环境中需要纯 Python + SQLite 的单机运行版，可随时切换到永久归档标签 [`v0.1.0-sqlite-lite`](https://github.com/osszhaoke/CTD-space/tree/v0.1.0-sqlite-lite)。

---

## 核心架构与功能

系统采用三容器轻量解耦架构，一键通过 Docker Compose 拉起：

```text
┌─────────────────┐       ┌─────────────────┐       ┌─────────────────┐
│     Next.js     │       │ Python 采集引擎  │       │  PostgreSQL 17  │
│   Web 驾驶舱     │ <───> │   (Collector)   │ ────> │   (Database)    │
│  (:3000 / API)  │       │ (AirGradient+8S)│       │  (JSONB 时序库)  │
└─────────────────┘       └─────────────────┘       └─────────────────┘
         ▲
         │ HTTP GET /api/v1/environment/window
         │ (供局域网 PTDS 节点拉取环境时间窗口快照)
┌─────────────────┐
│   PTDS 节点     │
│ (SomaLoop手环)  │
└─────────────────┘
```

1. **室内环境 · 空间空气与温湿（AirGradient）**：
   - 1Hz 真实采样，完整采集温度、相对湿度、CO₂、PM2.5、PM1.0、PM10、TVOC、NOx 等 29 个原始指标快照；
   - 具备权威参考基线角标（EPA 适度、WHO 日均 PM2.5、Sensirion VOC/NOx 基线），只做参考不制造焦虑；
   - 具备 11:50–12:00 等历史时间点悬停/选中交互回溯。
2. **床微环境 · 睡眠体征与温控（Eight Sleep）**：
   - 10 秒级定时采集，支持左右两侧实时床温（含 ≈ 摄氏度转换查表）、床面背部与腿部角度、音频播放器状态；
   - 完整采集睡眠评分、心率、HRV、呼吸率、睡眠分期及 30 天历史日数据断点续传（`eight_sleep_days`）；
   - 断连不闪断机制（`metadata.display` 持久化最后有效读数）。
3. **PTDS 跨节点 P0 环境时间窗口 API**：
   - 提供 `GET /api/v1/environment/window?source_id=...&start_at=...&end_at=...`；
   - 支持 PTDS 设备在观众体验结束后，一键拉取指定半开区间 `[start_at, end_at)` 内的四项环境平均值（温度、相对湿度、CO₂、PM2.5）与数据质量状态，用于合成体验纪念卡。
4. **全端毛玻璃视觉与 iPad Pro 投屏**：
   - 内置**深翠黑夜模式**（默认）与**暖白白昼模式**一键切换；
   - 响应式适配 iPad Pro 11" / 12.9"（横屏并排、竖屏自适应上下排列），支持全屏 Web App 投屏。

---

## 快速上手（Docker 一键部署）

### 1. 克隆代码与配置环境变量

```bash
git clone https://github.com/osszhaoke/CTD-space.git
cd CTD-space

# 复制环境变量模板
cp .env.example .env
```

在 `.env` 中填入 Eight Sleep 账号密码（只读权限）：

```dotenv
EIGHT_EMAIL=your_email@example.com
EIGHT_PASSWORD=your_password

# 可选：如果 AirGradient 在局域网特定 IP，可显式指定（未指定时走自动解析）
# AIRGRADIENT_HOST=192.168.1.100

# 房间名称（可选，默认 睡眠房）
ROOM_NAME=睡眠房
```

### 2. 一键启动服务

```bash
docker compose up -d --build
```

系统将自动完成：
- 启动 PostgreSQL 17 并自动执行 `init.sql` 建立表结构；
- 启动 Python 采集引擎，自动连接设备并向数据库持续写入；
- 启动 Next.js 独立服务，监听 `0.0.0.0:3000`。

### 3. 访问与使用

- **驾驶舱看板**：在浏览器或 iPad 打开 `http://<服务器IP>:3000`；
- **数据源与运维管理**：访问 `http://<服务器IP>:3000/manage`，可实时查看各设备采集频率、最后接收时间、原始 JSON 快照，或手动启停数据源；
- **停止服务**：
  ```bash
  docker compose down      # 停止服务并保留数据
  ```

---

## PTDS 跨节点对接 API（P0 契约）

供运行在另一台设备上的 PTDS 在会话结束时调用：

### 接口请求

`GET /api/v1/environment/window`

**请求参数：**
- `source_id`：环境设备标识（默认 `airgradient`）
- `start_at`：体验开始时间戳（UTC ISO-8601，如 `2026-10-21T01:00:00Z`）
- `end_at`：体验结束时间戳（UTC ISO-8601，如 `2026-10-21T01:05:00Z`）

**请求示例：**
```bash
curl -s "http://<CTDS_IP>:3000/api/v1/environment/window?source_id=airgradient&start_at=2026-10-21T01:00:00Z&end_at=2026-10-21T01:05:00Z"
```

**响应示例：**
```json
{
  "contract_version": "ctds.env-window/0.1-draft",
  "server_time": "2026-10-21T01:05:02.100Z",
  "source_id": "airgradient",
  "data_mode": "LIVE",
  "time_basis": "RECEIPT_ONLY",
  "start_at": "2026-10-21T01:00:00.000Z",
  "end_at": "2026-10-21T01:05:00.000Z",
  "interval": "[start_at,end_at)",
  "metrics": {
    "temperature": { "unit": "℃", "mean": 24.52, "sample_count": 5, "status": "CURRENT" },
    "humidity":    { "unit": "%RH", "mean": 48.20, "sample_count": 5, "status": "CURRENT" },
    "co2":         { "unit": "ppm", "mean": 680.00, "sample_count": 5, "status": "CURRENT" },
    "pm2_5":       { "unit": "µg/m³", "mean": 12.10, "sample_count": 5, "status": "CURRENT" }
  }
}
```

---

## 历史 SQLite 数据无损迁移工具

如果您之前运行过 `v0.1.0-sqlite-lite`，且希望将本地积累的 `ctds_c01.sqlite3` 历史时序导入到当前 PostgreSQL 库中：

```bash
# 在 Docker Compose 启动后运行迁移脚本
python3 tools/migrate_sqlite_to_pg.py --sqlite path/to/ctds_c01.sqlite3 --host 127.0.0.1 --port 5432
```

---

## 目录结构说明

```text
├── compose.yaml                # Docker Compose 一键启动文件
├── .env.example                # 环境变量配置模板
├── db/
│   └── init.sql                # PostgreSQL 幂等初始化脚本
├── collector/                  # Python 异步采集引擎 (AirGradient + Eight Sleep)
│   ├── Dockerfile              # Python 3.13-slim 镜像构建
│   ├── requirements.txt        # asyncpg, httpx 极简依赖
│   ├── store.py                # 数据库异步读写协议
│   ├── eight_api.py            # Eight Sleep 只读 API 客户端
│   └── sources/
│       ├── airgradient.py      # AirGradient 1Hz 采集与动态设备发现
│       └── eight_sleep.py      # Eight Sleep 状态采集与历史回填
├── web/                        # Next.js 15 空间看板与 API 服务
│   ├── Dockerfile              # Node.js Alpine 容器构建
│   ├── app/
│   │   ├── page.tsx            # 空间与床微环境双模块展示大屏
│   │   ├── manage/page.tsx     # 数据源运维与实时流水管理页
│   │   └── api/v1/environment/window/route.ts # PTDS 跨节点对接 API
│   └── components/
│       └── room-ui.tsx         # 卡片、趋势图与毛玻璃组件
└── tools/
    └── migrate_sqlite_to_pg.py # SQLite 到 PostgreSQL 历史数据迁移工具
```

---

## 开源协议

本项目采用 **[Apache License 2.0](LICENSE)** 协议开源。
