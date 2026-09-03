#!/bin/zsh
set -e

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT"

VERSION="0.2.0"
APP_NAME="ADB Tool"
APP_BUNDLE="$PROJECT/dist/$APP_NAME.app"
DMG="$PROJECT/dist/$APP_NAME-$VERSION.dmg"
STAGE="$(mktemp -d /tmp/adbtool_dmg.XXXXXX)"
trap 'rm -rf "$STAGE"' EXIT

echo "==> 1/2 用 py2app 构建 .app"
rm -rf "$PROJECT/build" "$APP_BUNDLE"
"$PROJECT/venv/bin/python" setup.py py2app >/dev/null 2>&1
[ -d "$APP_BUNDLE" ] || { echo "构建 .app 失败" >&2; exit 1; }

echo "==> 1.5 裁剪冗余 Qt 模块（qml/3D/多媒体等）"
"$PROJECT/scripts/trim_app.sh" "$APP_BUNDLE"

echo "==> 2/2 制作 .dmg（含 Applications 快捷入口）"
mkdir -p "$STAGE"
cp -R "$APP_BUNDLE" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

rm -f "$DMG"
hdiutil create -volname "$APP_NAME" -srcfolder "$STAGE" -ov -format UDRW "$DMG" >/dev/null

# 挂载并美化图标布局（非关键步骤，失败不影响 dmg 可用性）
MNT="$STAGE/mnt"
mkdir -p "$MNT"
if hdiutil attach -nobrowse -mountpoint "$MNT" "$DMG" >/dev/null 2>&1; then
  osascript >/dev/null 2>&1 <<APPLESCRIPT || true
tell application "Finder"
  tell disk "$APP_NAME"
    open
    set current view of container window to icon view
    set toolbar visible of container window to false
    set statusbar visible of container window to false
    set the bounds of container window to {100, 100, 520, 360}
    setViewOptions to the icon view options of container window
    set arrangement of setViewOptions to not arranged
    set icon size of setViewOptions to 96
    set position of item "$APP_NAME.app" of container window to {120, 150}
    set position of item "Applications" of container window to {380, 150}
    update without registering applications
    close
  end tell
end tell
APPLESCRIPT
  hdiutil detach "$MNT" >/dev/null 2>&1 || true
fi

# 压缩为最终 UDZO 镜像
FINAL="$PROJECT/dist/$APP_NAME-$VERSION-final.dmg"
rm -f "$FINAL"
hdiutil convert "$DMG" -format UDZO -o "$FINAL" >/dev/null
mv "$FINAL" "$DMG"

echo "已生成: $DMG"
echo "双击打开 dmg，把 $APP_NAME.app 拖到 Applications 即可完成安装。"
