#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE="$APP_ROOT/macos/Sift.js"
DIST_DIR="$APP_ROOT/dist"
OUTPUT="$DIST_DIR/Sift.app"
BUILD_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "$BUILD_DIR"
}
trap cleanup EXIT

mkdir -p "$DIST_DIR"

# Compile the repository path into the local launcher. The app can live in
# Applications or the Dock while continuing to run Sift from this checkout.
SIFT_BUILD_ROOT="$APP_ROOT" perl -pe \
  's/__APP_ROOT__/$ENV{SIFT_BUILD_ROOT}/g' \
  "$SOURCE" > "$BUILD_DIR/Sift.js"

rm -rf "$OUTPUT"
osacompile -l JavaScript -o "$OUTPUT" "$BUILD_DIR/Sift.js"

# Render the exact web SVG with macOS Quick Look. Its native SVG renderer
# preserves the gradients that some third-party converters flatten or darken.
if command -v qlmanage >/dev/null 2>&1 && [ -x "$APP_ROOT/.venv/bin/python" ]; then
  qlmanage -t -s 1024 -o "$BUILD_DIR" "$APP_ROOT/app/static/icon.svg" >/dev/null
  "$APP_ROOT/.venv/bin/python" "$APP_ROOT/macos/build_icon.py" \
    "$BUILD_DIR/icon.svg.png" "$OUTPUT/Contents/Resources/applet.icns"
fi

PLIST="$OUTPUT/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Set :CFBundleIconFile applet.icns" "$PLIST"
/usr/libexec/PlistBuddy -c "Delete :CFBundleIdentifier" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Delete :CFBundleShortVersionString" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Delete :CFBundleVersion" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string com.richardsuwandi.sift" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string 0.1.1" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleVersion string 2" "$PLIST"

# osacompile signs its original resources. Re-sign after replacing the icon and
# metadata so Finder accepts and displays the SVG-derived ICNS asset.
codesign --force --deep --sign - "$OUTPUT"
touch "$OUTPUT"

echo "Built $OUTPUT"
echo "The launcher runs Sift from $APP_ROOT"
