#!/bin/bash
# Build a double-clickable adbpush.app on the Desktop.
set -e

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$HOME/Desktop/adbpush.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>ADB Push</string>
    <key>CFBundleDisplayName</key>
    <string>ADB Push</string>
    <key>CFBundleIdentifier</key>
    <string>com.adbpush.app</string>
    <key>CFBundleExecutable</key>
    <string>adbpush</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>0.1.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.15</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/adbpush" <<SCRIPT
#!/bin/bash
LOG="/tmp/adbpush_app.log"
echo "[$(date)] launching" >> "\$LOG"
cd "$PROJECT" || { echo "cd failed: $PROJECT" >> "\$LOG"; exit 1; }
exec "$PROJECT/venv/bin/python" -m adbpush.main >> "\$LOG" 2>&1
SCRIPT

chmod +x "$APP/Contents/MacOS/adbpush"
echo "已创建: $APP"