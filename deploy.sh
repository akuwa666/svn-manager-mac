#!/bin/bash
# 开发用：改完 svnmanager.py 后跑这个，自动同步进已安装的 App 并重启
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
open "$APP"
echo "已更新并重启"
