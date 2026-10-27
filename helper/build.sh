#!/usr/bin/env bash
# Build "Soundscape Capture.app" (the audio capture helper) from source and ad-hoc sign it.
#   helper/build.sh [DEST_APP_PATH]
# Default destination: ${SOUNDSCAPE_HOME:-~/.soundscape}/SoundscapeCapture.app
# The app lives outside the plugin folder so macOS privacy permissions survive plugin updates.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${1:-${SOUNDSCAPE_HOME:-$HOME/.soundscape}/SoundscapeCapture.app}"
ARCH="$(uname -m)"

if xcrun --sdk macosx --find swiftc >/dev/null 2>&1; then
  SWIFTC=(xcrun --sdk macosx swiftc)
elif command -v swiftc >/dev/null 2>&1; then
  SWIFTC=(swiftc)
else
  echo "swiftc not found. Install the Xcode Command Line Tools: xcode-select --install" >&2
  exit 2
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
APP="$TMP/SoundscapeCapture.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

"${SWIFTC[@]}" -O -swift-version 5 -target "$ARCH-apple-macos13.0" \
  -o "$APP/Contents/MacOS/soundscape-capture" "$HERE/SoundscapeCapture.swift" \
  -framework CoreAudio -framework AudioToolbox -framework AVFoundation
cp "$HERE/Info.plist" "$APP/Contents/Info.plist"
cat "$HERE/SoundscapeCapture.swift" "$HERE/Info.plist" | shasum -a 256 | cut -d' ' -f1 \
  > "$APP/Contents/Resources/source.sha256"
codesign --force --sign - --identifier com.nealrau.soundscape.capture "$APP" >/dev/null 2>&1

mkdir -p "$(dirname "$DEST")"
rm -rf "$DEST"
mv "$APP" "$DEST"
echo "$DEST"
