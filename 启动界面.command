#!/bin/bash
# 双击这个文件就能打开图形界面（macOS）
cd "$(dirname "$0")" || exit 1

PORT=8765

# 服务已经在跑就直接把浏览器叫起来，别重复开
if /usr/bin/curl -s --max-time 1 "http://127.0.0.1:$PORT/api/ping" >/dev/null 2>&1; then
  echo "界面已经开着了，正在打开浏览器…"
  /usr/bin/open "http://127.0.0.1:$PORT/"
  exit 0
fi

# 用系统 python + 项目自带 vendor（摆脱对某个固定 Anaconda 路径的依赖）
PY="$(command -v python3)"

# 依赖没了就自动重装一次（vendor 不入库，换电脑时这里会自己恢复）
bash tools/ensure_deps.sh || { echo "依赖安装失败，请把终端输出发给维护者"; exit 1; }

export PYTHONPATH="$(pwd)/vendor${PYTHONPATH:+:$PYTHONPATH}"

echo "正在启动 PDF 编目界面…（关掉这个窗口或按 Ctrl-C 即停止）"
exec "$PY" -m pdfbookmark gui --port "$PORT"
