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

# 优先用装了 PyMuPDF 的那个 python
PY="/Users/zhangzhaoxing/anaconda3/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"

echo "正在启动 PDF 编目界面…（关掉这个窗口或按 Ctrl-C 即停止）"
exec "$PY" -m pdfbookmark gui --port "$PORT"
