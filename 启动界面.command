#!/bin/bash
# 双击这个文件就能打开图形界面（macOS）
cd "$(dirname "$0")" || exit 1
echo "正在启动 PDF 编目界面…"
exec python3 -m pdfbookmark gui
