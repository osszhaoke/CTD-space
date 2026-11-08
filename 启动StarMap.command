#!/usr/bin/env bash
# StarMap (CTDS) · 快速启动与后台常驻控制器（类似 PTDS 启动器）
# 双击直接启动后台静默数据采集并打开大屏；关闭窗口不会停止采集服务。
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# 查找可用 Python 3.9+ 解释器（优先 Homebrew，自动回退系统 Python）
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
  echo "[ERROR] 未找到 Python 3.9+ 运行环境，无法启动 StarMap 服务" >&2
  exit 1
fi

exec "$PY_BIN" -B delivery/scripts/service_control.py "$@"
