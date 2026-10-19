#!/usr/bin/env bash
# StarMap C01 · 睡眠房 Mac 一键部署、网域自动发现绑定与自动测试脚本
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

NO_OPEN=0
ES_EMAIL="${EIGHT_SLEEP_EMAIL:-}"
ES_PASSWORD="${EIGHT_SLEEP_PASSWORD:-}"
ES_TOKEN="${EIGHT_SLEEP_BEARER_TOKEN:-}"
ES_SIDE="${EIGHT_SLEEP_SIDE:-auto}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-open)
      NO_OPEN=1
      shift
      ;;
    --es-email)
      ES_EMAIL="${2:-}"
      shift 2
      ;;
    --es-password)
      ES_PASSWORD="${2:-}"
      shift 2
      ;;
    --es-token)
      ES_TOKEN="${2:-}"
      shift 2
      ;;
    --es-side)
      ES_SIDE="${2:-auto}"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done

echo "============================================================"
echo "  StarMap C01 · 睡眠房 Mac 一键部署与自检"
echo "============================================================"

# 1. 查找 Python 3.10+
PY_BIN=""
for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 || true)"; do
  if [[ -n "$cand" && -x "$cand" ]]; then
    if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      PY_BIN="$cand"
      break
    fi
  fi
done

if [[ -z "$PY_BIN" ]]; then
  echo "[ERROR] 未检测到 Python 3.10+，请先安装 Python 3。" >&2
  exit 1
fi
echo "[1/4] Python 环境就绪: $("$PY_BIN" --version) ($PY_BIN)"

# 2. 准备运行目录与冻结基线库（若新机首次运行）
mkdir -p var/ctds-c01
if [[ ! -f var/ctds-c01/ctds_c01.sqlite3 && -f var/ctds-c01/seed_frozen.sqlite3 ]]; then
  cp var/ctds-c01/seed_frozen.sqlite3 var/ctds-c01/ctds_c01.sqlite3
  echo "      已载入离线基准时序库 (11:50–12:00 冻结读数)"
fi
if [[ ! -f var/ctds-c01/config.json && -f var/ctds-c01/seed_config.json ]]; then
  cp var/ctds-c01/seed_config.json var/ctds-c01/config.json
fi

# 3. 局域网自动发现 AirGradient 并自助更新连接 + Eight Sleep 账号可选直连绑定
echo "[2/4] 正在扫描当前网段内的 AirGradient 监测器并检查 Eight Sleep 凭据..."
BIND_JSON="$(PYTHONPATH=development/ctds "$PY_BIN" -B -m room_display --config var/ctds-c01/config.json auto-bind)"
echo "      绑定结果: $BIND_JSON"

if [[ -n "$ES_TOKEN" || ( -n "$ES_EMAIL" && -n "$ES_PASSWORD" ) ]]; then
  echo "      检测到 Eight Sleep 凭据，正在执行云端只读鉴权与 6 项睡眠指标首次拉取..."
  ES_JSON="$(PYTHONPATH=development/ctds "$PY_BIN" -B -m room_display --config var/ctds-c01/config.json \
    --email "$ES_EMAIL" --password "$ES_PASSWORD" --token "$ES_TOKEN" --side "$ES_SIDE" --es-mode LIVE bind-eight-sleep)"
  echo "      Eight Sleep 接入结果: $ES_JSON"
fi

# 4. 自动执行全量单元与端到端自检测试
echo "[3/4] 正在执行自动化测试套件与大屏自检..."
TEST_JSON="$(PYTHONPATH=development/ctds "$PY_BIN" -B -m room_display --config var/ctds-c01/config.json self-test)"
echo "      测试结果: $TEST_JSON"

# 5. 重启后台大屏与持续采集守护进程（含断线重连自动重发现）
echo "[4/4] 正在启动本机环回大屏服务 (http://127.0.0.1:8765/)..."
if [[ -f var/ctds-c01/daemon.pid ]]; then
  OLD_PID="$(cat var/ctds-c01/daemon.pid 2>/dev/null || true)"
  if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    kill "$OLD_PID" 2>/dev/null || true
    sleep 0.5
  fi
fi
# 清理可能残留的同端口 room_display 进程
pkill -f "python3 -B -m room_display run" 2>/dev/null || true

nohup env PYTHONPATH=development/ctds "$PY_BIN" -B -m room_display --config var/ctds-c01/config.json run \
  > var/ctds-c01/daemon.log 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > var/ctds-c01/daemon.pid

READY=0
for _ in {1..20}; do
  if curl -fsS http://127.0.0.1:8765/readyz >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 0.25
done

if [[ "$READY" -ne 1 ]]; then
  echo "[ERROR] 服务未能在预期时间内就绪，请检查 var/ctds-c01/daemon.log" >&2
  exit 1
fi

echo "============================================================"
echo "  部署与自动测试全部通过！"
echo "  房间名称: 睡眠房 | 默认皮肤: 深翠黑夜模式"
echo "  大屏地址: http://127.0.0.1:8765/"
echo "  后台 PID: $NEW_PID"
echo "============================================================"

if [[ "$NO_OPEN" -eq 0 ]] && command -v open >/dev/null 2>&1; then
  open "http://127.0.0.1:8765/" || true
fi
