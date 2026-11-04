#!/bin/bash
# 生成 macOS 应用「PDF 编目.app」——双击开网页界面，把 PDF 拖到图标上直接编目。
#
# 用法：bash tools/make_app.sh [输出目录]
# 默认把 App 生成到项目根目录；想放桌面就把输出目录写在后面。

set -e
TOOLS="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(cd "$TOOLS/.." && pwd)"
OUT="${1:-$PROJECT}"
APP="$OUT/PDF编目.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cp "$TOOLS/AppInfo.plist" "$APP/Contents/Info.plist"
cp "$TOOLS/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"

# 把项目绝对路径写进启动脚本，这样 App 被拖到 /Applications 也能找到程序
sed "s|__PROJECT_DIR__|$PROJECT|g" "$TOOLS/app_launcher.sh" > "$APP/Contents/MacOS/launch"
chmod +x "$APP/Contents/MacOS/launch"

# 让访达认出来（图标、是否接受 PDF）
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$APP" 2>/dev/null || true

echo "已生成：$APP"
echo "把它拖到 Dock 或「应用程序」文件夹即可。"
