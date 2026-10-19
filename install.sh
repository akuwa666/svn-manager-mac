#!/bin/bash
# 安装 SVN Manager 到 ~/Applications
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Applications/SVN Manager.app"

if ! command -v svn >/dev/null 2>&1; then
  echo "✗ 没有找到 svn 命令"
  echo "  请先安装 Subversion： brew install subversion"
  exit 1
fi

pkill -f "SVN Manager.app/Contents/Resources/svnmanager.py" 2>/dev/null || true
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cp "$HERE/app/Info.plist" "$APP/Contents/Info.plist"
cp "$HERE/app/run" "$APP/Contents/MacOS/run"
chmod +x "$APP/Contents/MacOS/run"
cp "$HERE/svnmanager.py" "$APP/Contents/Resources/svnmanager.py"
[ -f "$HERE/assets/AppIcon.icns" ] && cp "$HERE/assets/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"

LSREG=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
[ -x "$LSREG" ] && "$LSREG" -f "$APP" 2>/dev/null || true

echo "✓ 已安装到: $APP"
echo "  双击打开，或在启动台搜索 \"SVN Manager\""
