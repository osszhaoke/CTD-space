#!/usr/bin/env bash
# StarMap C01 · 一键打包脚本
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

NO_PROMPT=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-prompt)
      NO_PROMPT=1
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
echo "  StarMap C01 · 睡眠房 Mac 一键打包"
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
echo "[1/5] Python 环境就绪: $("$PY_BIN" --version) ($PY_BIN)"

# 2. 检查 Eight Sleep 绑定状态（若未绑定且允许交互则询问）
ES_HAS_CRED="$(PYTHONPATH=development/ctds "$PY_BIN" -B -c '
import json
try:
    c = json.load(open("var/ctds-c01/config.json"))
    es = c.get("eight_sleep", {})
    has = bool(es.get("bearer_token") and str(es.get("bearer_token")).strip() and es.get("mode") == "LIVE")
    print("1" if has else "0")
except Exception:
    print("0")
')"

if [[ "$ES_HAS_CRED" -eq 0 && "$NO_PROMPT" -eq 0 ]]; then
  read -r -p "检测到本机尚未绑定 Eight Sleep，是否先绑定 Eight Sleep 再打包？[y/N] " yn || yn="n"
  if [[ "$yn" =~ ^[yY]$ ]]; then
    PYTHONPATH=development/ctds "$PY_BIN" -B -m room_display bind-eight-sleep
    ES_HAS_CRED="$(PYTHONPATH=development/ctds "$PY_BIN" -B -c '
import json
try:
    c = json.load(open("var/ctds-c01/config.json"))
    es = c.get("eight_sleep", {})
    has = bool(es.get("bearer_token") and str(es.get("bearer_token")).strip() and es.get("mode") == "LIVE")
    print("1" if has else "0")
except Exception:
    print("0")
')"
  else
    echo "      将生成未绑定 Eight Sleep 的现场包。"
  fi
fi

# 3. 运行全量测试（沙箱模式）；失败则退出，退出码不为 0，不生成包
#    跳过"解压现有旧包自检"这一项：旧包里的测试可能已过时，会把新包挡在门外（死锁）。
#    新包会在第 5 步构建后单独解压自检，通过后才替换正式交付包。
echo "[2/5] 正在运行全量自动化测试（沙箱隔离）..."
if ! env CTDS_TEST_SANDBOX=1 CTDS_SKIP_BUNDLE_TEST=1 PYTHONPATH=development/ctds "$PY_BIN" -B -m unittest discover -s development/ctds/tests/room_display >/dev/null 2>&1; then
  echo "[ERROR] 全量自动化测试失败，终止打包！" >&2
  exit 1
fi
echo "      测试套件全部通过 (PASS)。"

# 4. 运行 verify_manifest.py 校验（本地治理仓存在 manifest.lock.json 时强制校验；公开精简仓自动跳过）
if [[ -f delivery/manifest.lock.json && -f delivery/scripts/verify_manifest.py ]]; then
  echo "[3/5] 正在校验组合清单身份一致性..."
  if ! "$PY_BIN" -B delivery/scripts/verify_manifest.py >/dev/null 2>&1; then
    echo "[ERROR] 组合清单校验失败，终止打包！" >&2
    exit 1
  fi
  echo "      组合清单校验通过 (PASS)。"
else
  echo "[3/5] 公开精简版环境（无本地治理清单），跳过治理清单校验。"
fi

# 5. 先构建到暂存路径，解压自检通过后再原子替换正式交付包；失败则保留旧包不动
FINAL_ZIP="var/ctds-c01/StarMap-C01-Mac-OneClick.zip"
STAGE_ZIP="var/ctds-c01/.StarMap-C01-Mac-OneClick.staging.zip"
STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/starmap-bundle-check.XXXXXX")"
cleanup_stage() { rm -rf "$STAGE_DIR"; rm -f "$STAGE_ZIP"; }   # 暂存解压目录含凭据，务必清理
trap cleanup_stage EXIT

echo "[4/5] 正在构建单一交付与迁移包（暂存）..."
if ! env CTDS_BUNDLE_OUT="$STAGE_ZIP" "$PY_BIN" -B delivery/scripts/build_mac_bundle.py >/dev/null; then
  echo "[ERROR] 构建交付包失败，已保留原交付包。" >&2
  exit 1
fi
chmod 0600 "$STAGE_ZIP"

echo "[5/5] 正在解压新包并执行包内自检..."
if ! (cd "$STAGE_DIR" && unzip -q "$ROOT_DIR/$STAGE_ZIP" && cd StarMap-C01-Mac \
      && PYTHONPATH=development/ctds "$PY_BIN" -B -m room_display self-test >/dev/null 2>&1); then
  echo "[ERROR] 新交付包解压自检失败，已保留原交付包，未替换。" >&2
  exit 1
fi
mv -f "$STAGE_ZIP" "$FINAL_ZIP"
chmod 0600 "$FINAL_ZIP"
echo "      新包解压自检通过，已替换正式交付包。"

# 6. 获取摘要信息并打印
ZIP_PATH="var/ctds-c01/StarMap-C01-Mac-OneClick.zip"
ZIP_BYTES="$(stat -f%z "$ZIP_PATH" 2>/dev/null || wc -c < "$ZIP_PATH")"
ZIP_SHA="$("$PY_BIN" -B -c '
import hashlib, sys
p = sys.argv[1]
with open(p, "rb") as f:
    print(hashlib.sha256(f.read()).hexdigest())
' "$ZIP_PATH")"

AG_STATUS="$("$PY_BIN" -B -c '
import json
try:
    c = json.load(open("var/ctds-c01/config.json"))
    m = c.get("source", {}).get("mode", "SYNTHETIC")
    print("已绑定 (LIVE)" if m == "LIVE" else "未绑定 (SYNTHETIC)")
except Exception:
    print("未配置")
')"

ES_STATUS="未绑定"
if [[ "$ES_HAS_CRED" -eq 1 ]]; then
  ES_STATUS="已绑定"
fi

DB_SUMMARY="$("$PY_BIN" -B -c '
import sqlite3
from pathlib import Path
db = Path("var/ctds-c01/ctds_c01.sqlite3")
if not db.exists():
    print("数据库文件不存在")
else:
    conn = sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro&immutable=1", uri=True)
    cur = conn.cursor()
    cur.execute("SELECT source_id, count(*) FROM observation GROUP BY source_id")
    rows = cur.fetchall()
    cur.execute("SELECT count(*) FROM ingestion_attempt")
    att = cur.fetchone()[0]
    conn.close()
    parts = [f"{r[0]}: {r[1]} 行" for r in rows]
    parts.append(f"采集尝试: {att} 次")
    print(" | ".join(parts) if parts else "0 行")
')"

echo "============================================================"
echo "  打包成功完成！"
echo "  交付包路径:       $ZIP_PATH"
echo "  文件大小:         $ZIP_BYTES 字节 ($(( ZIP_BYTES / 1024 )) KB)"
echo "  包 SHA256:        $ZIP_SHA"
echo "  文件权限:         0600"
echo "  Eight Sleep 状态: $ES_STATUS"
echo "  AirGradient 状态: $AG_STATUS"
echo "  数据库来源统计:   $DB_SUMMARY"
echo "============================================================"
