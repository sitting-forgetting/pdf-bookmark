#!/bin/bash
# 确保项目依赖可用：没有就从随项目带的 wheels/ 里装到 vendor/（离线，几秒钟）
set -e
cd "$(dirname "$0")/.." || exit 1

PY="$(command -v python3)"

if "$PY" -c "import fitz, numpy, PIL" >/dev/null 2>&1; then
  echo "依赖已就绪（全局环境）"
  exit 0
fi
if [ -d vendor ] && PYTHONPATH="$(pwd)/vendor" "$PY" -c "import fitz, numpy, PIL" >/dev/null 2>&1; then
  echo "依赖已就绪（vendor/）"
  exit 0
fi

if [ ! -d wheels ] || [ -z "$(ls -A wheels/*.whl 2>/dev/null)" ]; then
  echo "缺少 wheels/ 缓存，无法离线安装；请联网后执行："
  echo "  python3 -m pip download --no-deps --dest wheels -r requirements.txt"
  exit 1
fi

echo "正在从本地 wheels/ 安装依赖到 vendor/（第一次，几秒钟）…"
"$PY" -m pip install --no-deps --no-index --find-links ./wheels \
  --target ./vendor -r requirements.txt 2>&1 | tail -6
echo "依赖安装完成。"
