#!/usr/bin/env bash
# Build a self-contained tbdtask AppImage. Defaults to the host arch; pass
# ARCH=aarch64 (with a Pi or qemu-user-static) to produce the Pi 400 build.
#
# Outputs: dist/tbdtask-<version>-<arch>.AppImage
#
# Approach: this is a pragmatic, dependency-light packaging path that does
# *not* require linuxdeploy or appimagetool to be installed system-wide. It
# bundles a portable Python interpreter (via python-build-standalone), the
# app source, and a launcher script into an AppDir, then assembles the
# AppImage with an embedded runtime.
#
# Required tools on the build host:
#   - bash, curl, tar, zstd
#   - patchelf (only needed if linuxdeploy is used; not used here)

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
ARCH="${ARCH:-$(uname -m)}"
PY_VERSION="${PY_VERSION:-3.11.9}"
APP_NAME="tbdtask"
APP_VERSION="$(grep -E '^version' "$ROOT/pyproject.toml" | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"
DIST="$ROOT/dist"
APPDIR="$DIST/${APP_NAME}.AppDir"

case "$ARCH" in
  x86_64)   PY_TRIPLE="x86_64-unknown-linux-gnu" ;;
  aarch64)  PY_TRIPLE="aarch64-unknown-linux-gnu" ;;
  *) echo "unsupported ARCH=$ARCH"; exit 1 ;;
esac

# python-build-standalone release URL pattern.
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20240415/cpython-${PY_VERSION}+20240415-${PY_TRIPLE}-install_only.tar.gz"

mkdir -p "$DIST"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/${APP_NAME}"

echo "[1/4] Fetching portable Python ($PY_VERSION, $ARCH)..."
PY_ARCHIVE="$DIST/python-${PY_VERSION}-${ARCH}.tar.gz"
if [[ ! -f "$PY_ARCHIVE" ]]; then
  curl -fL -o "$PY_ARCHIVE" "$PBS_URL"
fi
tar -xzf "$PY_ARCHIVE" -C "$APPDIR/usr"
mv "$APPDIR/usr/python" "$APPDIR/usr/python"

echo "[2/4] Installing app + dependencies into the bundle..."
"$APPDIR/usr/python/bin/python3" -m pip install --no-warn-script-location \
  fastapi 'uvicorn[standard]' sqlalchemy jinja2 python-multipart
cp -R "$ROOT/app" "$APPDIR/usr/share/${APP_NAME}/app"

echo "[3/4] Writing launcher and AppRun..."
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
export PYTHONPATH="$HERE/usr/share/tbdtask:$PYTHONPATH"
export TBDTASK_DATA_DIR="${TBDTASK_DATA_DIR:-${HOME}/.local/share/tbdtask}"
mkdir -p "$TBDTASK_DATA_DIR"
exec "$HERE/usr/python/bin/python3" -m app.main "$@"
EOF
chmod +x "$APPDIR/AppRun"

cat > "$APPDIR/${APP_NAME}.desktop" <<EOF
[Desktop Entry]
Name=tbdtask
Exec=AppRun
Icon=tbdtask
Type=Application
Categories=Office;
EOF

# Minimal placeholder icon (1x1 transparent PNG) so appimagetool is happy.
# Drop a proper PNG into resources/tbdtask.png to override.
if [[ -f "$ROOT/resources/tbdtask.png" ]]; then
  cp "$ROOT/resources/tbdtask.png" "$APPDIR/${APP_NAME}.png"
else
  printf '\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xfc\xff\xff?\x00\x05\xfe\x02\xfe\xa3\x35\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82' > "$APPDIR/${APP_NAME}.png"
fi

echo "[4/4] Assembling AppImage..."
APPIMAGETOOL="$DIST/appimagetool-$ARCH"
if [[ ! -x "$APPIMAGETOOL" ]]; then
  case "$ARCH" in
    x86_64)  AT_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage" ;;
    aarch64) AT_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-aarch64.AppImage" ;;
  esac
  curl -fL -o "$APPIMAGETOOL" "$AT_URL"
  chmod +x "$APPIMAGETOOL"
fi

OUT="$DIST/${APP_NAME}-${APP_VERSION}-${ARCH}.AppImage"
ARCH="$ARCH" "$APPIMAGETOOL" "$APPDIR" "$OUT"
echo "Built: $OUT"
