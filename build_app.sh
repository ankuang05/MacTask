#!/bin/bash
#
# Build MacTask.app — a double-clickable macOS app bundle.
#
# The bundle is lightweight: it ships the Python sources and launches them with
# the system python3 (PyObjC ships with macOS; pynput is auto-installed to the
# user site on first launch if missing). No compiler or py2app required.
#
# Usage:   ./build_app.sh          -> creates dist/MacTask.app
#
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HERE/dist/MacTask.app"
VERSION="1.0.0"

echo "Building MacTask.app ..."
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# --- Python sources ---
cp "$HERE/mactask_app.py"    "$APP/Contents/Resources/"
cp "$HERE/mactask_worker.py" "$APP/Contents/Resources/"

# --- Info.plist ---
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>              <string>MacTask</string>
  <key>CFBundleDisplayName</key>       <string>MacTask</string>
  <key>CFBundleIdentifier</key>        <string>com.mactask.app</string>
  <key>CFBundleExecutable</key>        <string>MacTask</string>
  <key>CFBundlePackageType</key>       <string>APPL</string>
  <key>CFBundleShortVersionString</key><string>${VERSION}</string>
  <key>CFBundleVersion</key>           <string>${VERSION}</string>
  <key>LSMinimumSystemVersion</key>    <string>11.0</string>
  <key>NSHighResolutionCapable</key>   <true/>
  <key>LSApplicationCategoryType</key> <string>public.app-category.utilities</string>
</dict>
</plist>
PLIST

# --- Launcher ---
cat > "$APP/Contents/MacOS/MacTask" <<'SH'
#!/bin/bash
DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"
PY="$(command -v python3 || echo /usr/bin/python3)"
# Ensure the one runtime dependency is present (PyObjC already ships with macOS).
"$PY" -c "import pynput" 2>/dev/null || "$PY" -m pip install --user pynput >/dev/null 2>&1 || true
exec "$PY" "$DIR/mactask_app.py"
SH
chmod +x "$APP/Contents/MacOS/MacTask"

echo "Done: $APP"
echo "Open it with:  open \"$APP\""
