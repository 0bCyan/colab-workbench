#!/bin/bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null; then
  printf '需要先安装 uv，再运行此脚本。\n'
  exit 1
fi
if ! command -v node >/dev/null; then
  printf '需要 Node.js 22 或更新版本。\n'
  exit 1
fi
uv sync --frozen
if [ ! -d frontend/node_modules ]; then
  npm --prefix frontend ci
fi
npm --prefix frontend run build
printf '\nFieldwork: http://127.0.0.1:8765\n按 Ctrl+C 正常退出；按设置释放工作台创建的机器。\n\n'
exec uv run uvicorn backend.app:app --host 127.0.0.1 --port 8765 --timeout-graceful-shutdown 420
