#!/bin/bash
# Builds David.app into /Applications. Run again after changing app/David.swift.
# Signed with a certificate made on this Mac (see below), so macOS keeps David's
# permissions across rebuilds.
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

# macOS ties permissions to the app's signature. Ad-hoc signing changes it on every
# build, silently voiding Screen Recording and the rest; a certificate keeps it the same.
# The certificate and its key exist only in this Mac's login keychain.
CERT="David Local Signing"
if ! security find-certificate -c "$CERT" >/dev/null 2>&1; then
  echo "Making the '$CERT' certificate (once)…"
  TMP="$(mktemp -d)"
  /usr/bin/openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=$CERT" \
    -keyout "$TMP/key.pem" -out "$TMP/cert.pem" \
    -addext "keyUsage=critical,digitalSignature" -addext "extendedKeyUsage=critical,codeSigning" 2>/dev/null
  /usr/bin/openssl pkcs12 -export -inkey "$TMP/key.pem" -in "$TMP/cert.pem" -out "$TMP/d.p12" -passout pass:david
  security import "$TMP/d.p12" -k ~/Library/Keychains/login.keychain-db -P david -T /usr/bin/codesign >/dev/null
  rm -rf "$TMP"
fi
codesign --force --sign "$CERT" "$APP"
echo "Built $APP"
