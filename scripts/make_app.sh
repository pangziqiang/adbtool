set -e

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$HOME/Desktop/adbtool.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
mkdir -p "$APP/Contents/Resources"
cp "$PROJECT/assets/adbtool.icns" "$APP/Contents/Resources/adbtool.icns" 2>/dev/null || true

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>ADB Tool</string>
    <key>CFBundleDisplayName</key>
    <string>ADB Tool</string>
    <key>CFBundleIdentifier</key>
    <string>com.adbtool.app</string>
    <key>CFBundleExecutable</key>
    <string>adbtool</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>0.1.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>CFBundleIconFile</key>
    <string>adbtool</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.15</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/adbtool" <<'LAUNCHER'
#!/bin/bash
LOG="/tmp/adbtool_app.log"
echo "[$(date)] launching" >> "$LOG"
cd "/Volumes/winandmac/adbtool" || { echo "cd failed" >> "$LOG"; exit 1; }
exec "/Volumes/winandmac/adbtool/venv/bin/python" -c "
import sys, os
os.chdir('PROJECT_PATH_PLACEHOLDER')
from adbtool.main import main
main()
" >> "$LOG" 2>&1
LAUNCHER

# Fix the placeholder with actual project path
sed -i '' "s|PROJECT_PATH_PLACEHOLDER|$PROJECT|g" "$APP/Contents/MacOS/adbtool"

chmod +x "$APP/Contents/MacOS/adbtool"
echo "已创建: $APP"
