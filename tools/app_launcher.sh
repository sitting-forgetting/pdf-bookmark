#!/bin/bash
# PDF 编目 · macOS 启动器
#
# 双击：打开网页界面（如果服务没在跑就先拉起来）
# 把 PDF 拖到这个图标上：直接生成书签，完成后在访达里定位结果

APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"

# 项目位置：优先用打包时写死的绝对路径，找不到再按相对位置推
PROJECT="__PROJECT_DIR__"
if [ ! -d "$PROJECT/pdfbookmark" ]; then
  PROJECT="$(cd "$APP_DIR/.." && pwd)"
fi

PY="/Users/zhangzhaoxing/anaconda3/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"

PORT=8765
OUT_DIR="$PROJECT/out/拖入"

say() {  # 对话框：只有真的需要人看到的才用
  [ -n "$PDFBOOKMARK_SILENT" ] && return 0
  /usr/bin/osascript -e "display dialog \"$1\" with title \"PDF 编目\" buttons {\"好\"} default button 1" >/dev/null 2>&1
}

note() {  # 通知：不打断操作
  [ -n "$PDFBOOKMARK_SILENT" ] && return 0
  /usr/bin/osascript -e "display notification \"$1\" with title \"PDF 编目\"" >/dev/null 2>&1
}

reveal() { [ -n "$PDFBOOKMARK_SILENT" ] || /usr/bin/open -R "$1"; }
open_folder() { [ -n "$PDFBOOKMARK_SILENT" ] || /usr/bin/open "$1"; }

if [ ! -d "$PROJECT/pdfbookmark" ]; then
  say "找不到程序目录，请把这个 App 放在 pdf-bookmark 文件夹里，或重新生成一次。"
  exit 1
fi

# macOS 会拦住 App 读取「桌面/文稿/下载」这些受保护目录。
# 先探一下能不能读到项目里的文件，读不到就直接给用户指路，别让他对着没反应的窗口干等。
if ! /bin/ls "$PROJECT/pdfbookmark" >/dev/null 2>&1; then
  if [ -z "$PDFBOOKMARK_SILENT" ]; then
    /usr/bin/osascript <<'APPLESCRIPT' >/dev/null 2>&1
display dialog "macOS 阻止了「PDF 编目」读取桌面上的文件。

请在「系统设置 → 隐私与安全性 → 完全磁盘访问权限」里勾选 PDF 编目，
然后重新打开它（只需设置一次）。

点「去设置」会自动打开那个面板。" buttons {"去设置", "取消"} default button 1 with icon caution with title "PDF 编目"
APPLESCRIPT
    if [ $? -eq 0 ]; then
      /usr/bin/open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
    fi
  fi
  exit 1
fi

cd "$PROJECT" || exit 1

# ---------------------------------------------------------------- 收集参数
files=()
for arg in "$@"; do
  case "$arg" in
    -psn_*) continue ;;          # 旧版 Finder 会塞这个
  esac
  if [ -f "$arg" ]; then
    files+=("$arg")
  fi
done

# ---------------------------------------------------------------- 双击：开界面
if [ ${#files[@]} -eq 0 ]; then
  if ! /usr/bin/curl -s --max-time 1 "http://127.0.0.1:$PORT/api/ping" >/dev/null 2>&1; then
    nohup "$PY" -m pdfbookmark gui --port "$PORT" --no-open > /tmp/pdfbookmark-gui.log 2>&1 &
    for _ in $(seq 1 20); do
      sleep 0.5
      /usr/bin/curl -s --max-time 1 "http://127.0.0.1:$PORT/api/ping" >/dev/null 2>&1 && break
    done
  fi
  /usr/bin/open "http://127.0.0.1:$PORT/"
  exit 0
fi

# ---------------------------------------------------------------- 拖进来：直接处理
mkdir -p "$OUT_DIR"
ok=0; skip=0; fail=0; failed_names=""

for f in "${files[@]}"; do
  base="$(basename "$f")"
  note "正在处理：$base"
  log="$("$PY" -m pdfbookmark run "$f" -o "$OUT_DIR" 2>&1)"
  code=$?
  case $code in
    0)
      if printf '%s' "$log" | grep -q "默认不覆盖"; then
        skip=$((skip+1))
      else
        ok=$((ok+1))
      fi
      ;;
    *)
      fail=$((fail+1))
      failed_names="$failed_names\n· $base"
      ;;
  esac
done

# 成功的话定位到最后一个成品
last_pdf="$(ls -t "$OUT_DIR"/*.bookmarked.pdf 2>/dev/null | head -1)"
if [ "$ok" -gt 0 ] && [ -n "$last_pdf" ]; then
  reveal "$last_pdf"
elif [ "$skip" -gt 0 ] && [ "$fail" -eq 0 ]; then
  open_folder "$OUT_DIR"
fi

summary="成功 $ok 本"
[ "$skip" -gt 0 ] && summary="$summary，跳过 $skip 本（已有书签）"
[ "$fail" -gt 0 ] && summary="$summary，失败 $fail 本$failed_names"

if [ "$fail" -gt 0 ]; then
  say "$summary\n\n输出目录：$OUT_DIR"
else
  note "$summary"
fi
exit 0
