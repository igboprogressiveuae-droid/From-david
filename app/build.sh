#!/bin/bash
# Builds David.app into /Applications. Run again after changing app/David.swift.
# Rebuilding gives the app a new signature, so macOS may ask for its permissions again.
set -euo pipefail
cd "$(dirname "$0")"
REPO="$(cd .. && pwd)"
APP="/Applications/David.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
swiftc -O -o "$APP/Contents/MacOS/David" David.swift -framework Cocoa -framework WebKit
cp David.icns "$APP/Contents/Resources/David.icns"

# DavidFolder: where the app finds David's code. It lives only in the built app, never in git.
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>David</string>
  <key>CFBundleDisplayName</key><string>David</string>
  <key>CFBundleIdentifier</key><string>tech.aiby.david</string>
  <key>CFBundleExecutable</key><string>David</string>
  <key>CFBundleIconFile</key><string>David</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>David listens for "Wake up David" and to what you say to him.</string>
  <key>NSAppleEventsUsageDescription</key><string>David opens, controls and reads apps when you ask him to.</string>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>DavidFolder</key><string>$REPO</string>
</dict>
</plist>
PLIST

# Signed for this Mac only, so its permissions stick between launches.
codesign --force --sign - "$APP"
echo "Built $APP"
