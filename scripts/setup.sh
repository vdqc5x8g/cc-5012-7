#!/usr/bin/env bash
# One-time setup: install node dependencies (React + ffmpeg-static) and build
# the frontend. Fonts are already committed under assets/fonts.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v node >/dev/null 2>&1; then
  echo "需要 Node.js 18+。" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "需要 Python 3.10+。" >&2
  exit 1
fi

echo "==> 安装 ffmpeg-static（提供 FFmpeg 可执行文件）"
npm install --no-audit --no-fund --prefix "$ROOT"

echo "==> 安装前端依赖"
npm install --no-audit --no-fund --prefix "$ROOT/frontend"

echo "==> 构建前端"
npm run build --prefix "$ROOT/frontend"

FF="${ROOT}/node_modules/ffmpeg-static/ffmpeg"
if [[ -x "$FF" ]]; then
  echo "FFmpeg: $FF"
else
  echo "警告：未找到 $FF。可设置 SIGSUB_FFMPEG 指向系统 ffmpeg。" >&2
fi

echo "完成。启动图形入口：npm run serve --prefix $ROOT/frontend"
