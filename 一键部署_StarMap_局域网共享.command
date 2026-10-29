#!/usr/bin/env bash
# StarMap C01 · 睡眠房 Mac 局域网共享一键部署双击入口
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
exec /bin/bash delivery/scripts/deploy_mac.sh --lan "$@"
