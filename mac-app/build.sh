#!/bin/bash
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
dist="$root/dist"
app="$dist/Ask Agents.app"
mkdir -p "$dist"
build="$(mktemp -d "$dist/.ask-agents-build.XXXXXX")"
staged="$build/Ask Agents.app"
backup="$build/previous.app"
cleanup() {
    if [[ -d "$backup" && ! -e "$app" ]]; then
        mv "$backup" "$app"
    fi
    rm -rf "$build"
}
trap cleanup EXIT

mkdir -p "$staged/Contents/MacOS" "$staged/Contents/Resources/ask_agents"
cp "$root/mac-app/Info.plist" "$staged/Contents/Info.plist"
cp "$root/ask_agents/"*.py "$staged/Contents/Resources/ask_agents/"

sdk="$(xcrun --sdk macosx --show-sdk-path)"
xcrun --sdk macosx swiftc -O -parse-as-library -sdk "$sdk" -target "$(uname -m)-apple-macos14.0" \
    "$root/mac-app/Sources/"*.swift -o "$staged/Contents/MacOS/AskAgents"

iconset="$build/AppIcon.iconset"
mkdir "$iconset"
xcrun --sdk macosx swift "$root/mac-app/MakeIcon.swift" "$build/icon.png"
for size in 16 32 128 256 512; do
    /usr/bin/sips -z "$size" "$size" "$build/icon.png" --out "$iconset/icon_${size}x${size}.png" >/dev/null
    /usr/bin/sips -z "$((size * 2))" "$((size * 2))" "$build/icon.png" --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
done
/usr/bin/iconutil -c icns "$iconset" -o "$staged/Contents/Resources/AppIcon.icns"

/usr/bin/codesign --force --sign - "$staged"
/usr/bin/codesign --verify --strict "$staged"

if [[ -e "$app" || -L "$app" ]]; then
    identifier=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$app/Contents/Info.plist" 2>/dev/null || true)
    if [[ -L "$app" || "$identifier" != "local.ask-agents.desktop" ]]; then
        printf '目标路径已有其他文件：%s\n' "$app" >&2
        exit 1
    fi
    mv "$app" "$backup"
fi
mv "$staged" "$app"
printf '%s\n' "$app"
