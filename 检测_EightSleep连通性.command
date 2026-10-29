#!/usr/bin/env bash
# StarMap C01 · Eight Sleep 连通性与运行环境诊断启动器
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

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
  echo "[ERROR] 未检测到 Python 3.9+ 运行时。" >&2
  exit 1
fi

exec "$PY_BIN" -B delivery/scripts/diag_eight_sleep.py "$@"
