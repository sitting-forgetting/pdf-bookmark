#!/bin/bash
# PDF 编目 · macOS 启动器
#
# 为什么这么简单：App 是脚本包，没法做代码签名，macOS 的 TCC 不认它，
# 直接读桌面/文稿/下载里的文件会被静默拒绝（表现就是双击没反应）。
# 所以 App 只负责把界面打开——真正的文件读取交给终端（有正规签名），
# PDF 则在网页里拖进去（浏览器自己的权限，不受影响）。

APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"

PROJECT="__PROJECT_DIR__"
if [ ! -d "$PROJECT/pdfbookmark" ]; then
  PROJECT="$(cd "$APP_DIR/.." && pwd)"
fi

/usr/bin/open -a Terminal "$PROJECT/启动界面.command"
exit 0
