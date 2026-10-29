#!/usr/bin/env bash
# StarMap C01 · 睡眠房 Mac 一键部署、网域自动发现绑定与自动测试脚本
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

NO_OPEN=0
NO_PROMPT=0
ALLOW_LAN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-open)
      NO_OPEN=1
      shift
      ;;
    --no-prompt)
      NO_PROMPT=1
      shift
      ;;
    --lan|--share-lan)
      ALLOW_LAN=1
      shift
      ;;
    *)
      shift
      ;;
  esac
done

if [[ ! -t 0 ]]; then
  NO_PROMPT=1
fi

echo "============================================================"
echo "  StarMap C01 · 睡眠房 Mac 一键部署与自检"
echo "============================================================"

# 1. 查找 Python 3.9+（macOS 自带的 /usr/bin/python3 3.9 即可）
PY_BIN=""
for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 || true)" /usr/bin/python3; do
  if [[ -n "$cand" && -x "$cand" ]]; then
    if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      PY_BIN="$cand"
      break
    fi
  fi
done

if [[ -z "$PY_BIN" ]]; then
  echo "[ERROR] 未检测到 Python 3.9+。请在终端运行 xcode-select --install 安装命令行工具（自带 Python 3），或安装 Homebrew 版 Python 后重试。" >&2
  exit 1
fi
echo "[1/4] Python 环境就绪: $("$PY_BIN" --version) ($PY_BIN)"

# 检查 Python HTTPS 证书链（自动解决 macOS python.org 安装包未装证书的问题）
# 只有"证书校验失败"才修复：HTTP 404 等状态码说明 TLS 已通过；断网/超时不归因于证书。
# 运行时 adapter.get_ssl_context() 另有系统 /etc/ssl/cert.pem 兜底，此处仅为让本机 Python 也恢复正常。
TLS_PROBE='
import ssl, sys, urllib.error, urllib.request
try:
    urllib.request.urlopen("https://auth-api.8slp.net", timeout=5)
except urllib.error.HTTPError:
    pass
except urllib.error.URLError as exc:
    sys.exit(3 if isinstance(exc.reason, ssl.SSLCertVerificationError) else 0)
except ssl.SSLCertVerificationError:
    sys.exit(3)
except Exception:
    pass
'
TLS_RC=0
"$PY_BIN" -c "$TLS_PROBE" >/dev/null 2>&1 || TLS_RC=$?
if [[ "$TLS_RC" -eq 3 ]]; then
  PY_VER="$("$PY_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo '')"
  CERT_CMD="/Applications/Python ${PY_VER}/Install Certificates.command"
  if [[ -n "$PY_VER" && -f "$CERT_CMD" ]]; then
    echo "      检测到 Python 缺少根证书（python.org 安装版常见），正在自动安装..."
    bash "$CERT_CMD" >/dev/null 2>&1 || true
    if "$PY_BIN" -c "$TLS_PROBE" >/dev/null 2>&1; then
      echo "      根证书已安装，Eight Sleep HTTPS 校验通过。"
    else
      echo "      [提示] 自动安装未生效；服务将使用 macOS 系统根证书兜底，如仍无法连接请运行诊断脚本。"
    fi
  else
    echo "      [提示] 本机 Python 缺少根证书；服务将使用 macOS 系统根证书兜底。"
  fi
fi

# 2. 准备运行目录、私密配置与历史数据库
mkdir -p var/ctds-c01
if [[ ! -f var/ctds-c01/config.json ]]; then
  if [[ -f var/ctds-c01/config.example.json ]]; then
    cp var/ctds-c01/config.example.json var/ctds-c01/config.json
  elif [[ -f var/ctds-c01/seed_config.json ]]; then
    cp var/ctds-c01/seed_config.json var/ctds-c01/config.json
  else
    if ! PYTHONPATH=development/ctds "$PY_BIN" -B -m room_display --config var/ctds-c01/config.json init-config; then
      echo "[ERROR] 初始化默认配置文件失败，请检查环境或权限。" >&2
      exit 1
    fi
  fi
fi
if ! PYTHONPATH=development/ctds "$PY_BIN" -B -m room_display --config var/ctds-c01/config.json migrate-config; then
  echo "[ERROR] 迁移或规范化现有配置文件失败，请检查 var/ctds-c01/config.json。" >&2
  exit 1
fi

# 3. 局域网自动发现 AirGradient 并自助更新连接
echo "[2/4] 正在扫描当前网段内的 AirGradient 监测器..."
BIND_JSON="$(PYTHONPATH=development/ctds "$PY_BIN" -B -m room_display --config var/ctds-c01/config.json auto-bind)"
echo "      绑定结果: $BIND_JSON"

AG_MODE="$("$PY_BIN" -B -c '
import json
try:
    c = json.load(open("var/ctds-c01/config.json"))
    print(c.get("source", {}).get("mode", "SYNTHETIC"))
except Exception:
    print("SYNTHETIC")
')"

if [[ "$AG_MODE" != "LIVE" && "$NO_PROMPT" -eq 0 ]]; then
  read -r -p "未发现 AirGradient，可手动输入设备 IP（留空跳过）：" ag_ip || ag_ip=""
  ag_ip="$(echo "$ag_ip" | tr -d '[:space:]')"
  if [[ -n "$ag_ip" ]]; then
    SET_RES="$("$PY_BIN" -B -c '
import sys, json
from room_display import adapter, config
ip = sys.argv[1]
if not ip.startswith("http://") and not ip.startswith("https://"):
    ip = "http://" + ip
p = config.DEFAULT_CONFIG
try:
    adapter.validate_base_url(ip)
    c = config.load(p)
    c["source"]["base_url"] = ip
    c["source"]["mode"] = "LIVE"
    c["source"]["binding_revision"] = int(c["source"].get("binding_revision", 1)) + 1
    config.save_config(config.validate(c), p)
    print(json.dumps({"success": True}))
except Exception as e:
    print(json.dumps({"success": False, "error": str(e)}))
' "$ag_ip")"
    if echo "$SET_RES" | grep -q '"success": true'; then
      echo "      已配置 AirGradient 地址: $ag_ip"
      AG_MODE="LIVE"
    else
      echo "      [WARN] IP 格式无效，跳过手动配置: $SET_RES"
    fi
  fi
fi

# 检查 Eight Sleep 凭据与连通状态 (I1)
ES_HAS_CRED="$("$PY_BIN" -B -c '
import json
try:
    c = json.load(open("var/ctds-c01/config.json"))
    es = c.get("eight_sleep", {})
    has = bool(es.get("bearer_token") or es.get("refresh_token") or (es.get("email") and es.get("password")))
    print("1" if has else "0")
except Exception:
    print("0")
')"

ES_DISPLAY_STATUS="待接入"

if [[ "$ES_HAS_CRED" -eq 1 ]]; then
  echo "      检测到包内已有 Eight Sleep 凭据，正在静默验证连通性..."
  CHECK_RES="$(PYTHONPATH=development/ctds "$PY_BIN" -B -m room_display --config var/ctds-c01/config.json bind-eight-sleep --check 2>/dev/null || true)"
  if echo "$CHECK_RES" | grep -q '"outcome": "OK"'; then
    echo "      Eight Sleep: 已接入 (LIVE)，验证成功。"
    ES_DISPLAY_STATUS="在线"
  elif echo "$CHECK_RES" | grep -q '"outcome": "NO_CONNECTION"'; then
    echo "      Eight Sleep: 网络不可达，服务启动后会自动重试。"
    ES_DISPLAY_STATUS="网络离线（自动重试）"
  else
    echo "      Eight Sleep: 认证失败。"
    ES_DISPLAY_STATUS="认证失效"
    if [[ "$NO_PROMPT" -eq 0 ]]; then
      read -r -p "Eight Sleep 认证失败，是否重新输入账号密码？[Y/n] " yn || yn="y"
      yn="${yn:-y}"
      if [[ "$yn" =~ ^[yY]$ ]]; then
        if PYTHONPATH=development/ctds "$PY_BIN" -B -m room_display --config var/ctds-c01/config.json bind-eight-sleep; then
          ES_DISPLAY_STATUS="在线"
        else
          ES_DISPLAY_STATUS="认证失效"
        fi
      fi
    fi
  fi
else
  # 包内没有凭据
  if [[ "$NO_PROMPT" -eq 0 ]]; then
    read -r -p "是否现在接入 Eight Sleep？[Y/n] " yn || yn="y"
    yn="${yn:-y}"
    if [[ "$yn" =~ ^[yY]$ ]]; then
      if PYTHONPATH=development/ctds "$PY_BIN" -B -m room_display --config var/ctds-c01/config.json bind-eight-sleep; then
        MODE_NOW="$("$PY_BIN" -B -c '
import json
try:
    c = json.load(open("var/ctds-c01/config.json"))
    print(c.get("eight_sleep", {}).get("mode", "READY"))
except Exception:
    print("READY")
')"
        if [[ "$MODE_NOW" == "LIVE" ]]; then
          ES_DISPLAY_STATUS="在线"
        else
          ES_DISPLAY_STATUS="待接入"
        fi
      else
        ES_DISPLAY_STATUS="认证失效"
      fi
    else
      echo "      已跳过 Eight Sleep 接入，保持 READY 待接入状态。"
      ES_DISPLAY_STATUS="待接入"
    fi
  else
    ES_DISPLAY_STATUS="待接入"
  fi
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
    if ps -p "$OLD_PID" -o command= 2>/dev/null | grep -q "room_display"; then
      echo "      检测到运行中的旧实例 (PID $OLD_PID)，正在精确终止..."
      kill "$OLD_PID" 2>/dev/null || true
      for _ in {1..20}; do
        if ! kill -0 "$OLD_PID" 2>/dev/null; then
          break
        fi
        sleep 0.2
      done
      if kill -0 "$OLD_PID" 2>/dev/null; then
        kill -9 "$OLD_PID" 2>/dev/null || true
      fi
    else
      echo "      PID $OLD_PID 未匹配 room_display 进程，跳过终止以防误杀。"
    fi
  fi
  rm -f var/ctds-c01/daemon.pid
fi

# 检查端口 8765 是否被遗留 room_display 实例占用（消除 pid 文件不同步隐患）
if command -v lsof >/dev/null 2>&1; then
  # 只取监听进程（不含浏览器等客户端连接），可能多行，逐个核对命令行后再终止
  for PORT_PID in $(lsof -nP -ti tcp:8765 -sTCP:LISTEN 2>/dev/null || true); do
    if kill -0 "$PORT_PID" 2>/dev/null && ps -p "$PORT_PID" -o command= 2>/dev/null | grep -q "room_display"; then
      echo "      检测到端口 8765 监听中的实例 (PID $PORT_PID)，正在精确终止..."
      kill "$PORT_PID" 2>/dev/null || true
      for _ in {1..20}; do
        if ! kill -0 "$PORT_PID" 2>/dev/null; then
          break
        fi
        sleep 0.2
      done
      if kill -0 "$PORT_PID" 2>/dev/null; then
        kill -9 "$PORT_PID" 2>/dev/null || true
      fi
    fi
  done
fi

EXTRA_RUN_ARGS=""
if [[ "$ALLOW_LAN" -eq 1 ]]; then
  EXTRA_RUN_ARGS="--lan"
fi

nohup env PYTHONPATH=development/ctds "$PY_BIN" -B -m room_display --config var/ctds-c01/config.json run $EXTRA_RUN_ARGS \
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

# 获取本机局域网 IP（用于多设备共享提示）
LAN_IP=""
if command -v ipconfig >/dev/null 2>&1; then
  LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
fi
if [[ -z "$LAN_IP" ]]; then
  LAN_IP="$("$PY_BIN" -c '
import socket
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    print(s.getsockname()[0])
    s.close()
except Exception:
    print("")
' 2>/dev/null || echo '')"
fi

# 以运行中服务报告的真实连接状态为准（仅 mode=LIVE 不代表已连上设备）
AG_DISPLAY_STATUS="$(curl -fsS "http://127.0.0.1:8765/api/v1/rooms/$(curl -fsS http://127.0.0.1:8765/api/v1/rooms 2>/dev/null | "$PY_BIN" -c 'import json,sys; print(json.load(sys.stdin)["room_id"])' 2>/dev/null)/display" 2>/dev/null | "$PY_BIN" -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("未知（服务未响应）"); sys.exit()
if d.get("source_mode") != "LIVE":
    print("合成测试数据")
elif d.get("actual_source_connectivity") == "CONNECTED":
    print("在线")
else:
    print("未发现设备 · 服务将持续自动扫描（设备上线后自动接入）")
' 2>/dev/null || echo "未知")"

echo "============================================================"
echo "  StarMap C01 · 部署与自检完成"
echo "  AirGradient 状态: $AG_DISPLAY_STATUS"
echo "  Eight Sleep 状态: $ES_DISPLAY_STATUS"
echo "  本机访问地址:     http://127.0.0.1:8765/"
if [[ -n "$LAN_IP" ]]; then
  if [[ "$ALLOW_LAN" -eq 1 ]]; then
    echo "  局域网共享地址:   http://${LAN_IP}:8765/ (已启用，同Wi-Fi设备均可访问)"
  else
    echo "  局域网共享地址:   http://${LAN_IP}:8765/ (若需同Wi-Fi其他设备访问，请双击运行'一键部署_StarMap_局域网共享.command')"
  fi
fi
echo "  后台 PID:         $NEW_PID"
echo "============================================================"

if [[ "$NO_OPEN" -eq 0 ]] && command -v open >/dev/null 2>&1; then
  open "http://127.0.0.1:8765/" || true
fi
