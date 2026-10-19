#!/bin/bash
# 开发用：改完代码后同步进已安装的 App 并重启服务。
# 复用启动器的单例逻辑：已有窗口只刷新，不会新开窗口。
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Applications/SVN Manager.app"

if [ ! -d "$APP" ]; then
  echo "还没安装，先运行 ./install.sh"
  exit 1
fi

pkill -f "SVN Manager.app/Contents/Resources/svnmanager.py" 2>/dev/null || true
sleep 0.6

cp "$HERE/svnmanager.py" "$APP/Contents/Resources/svnmanager.py"
cp "$HERE/app/run" "$APP/Contents/MacOS/run"
cp "$HERE/app/Info.plist" "$APP/Contents/Info.plist"
chmod +x "$APP/Contents/MacOS/run"

"$APP/Contents/MacOS/run"
echo "已更新（窗口已刷新）"
