#!/usr/bin/env bash
# Build a self-contained tbdtask AppImage for the Raspberry Pi 400.
#
# Defaults to ARCH=aarch64 (Pi 400). Override with ARCH=x86_64 for desktop
# testing. The build host can differ from the target architecture: this
# script fetches the matching python-build-standalone interpreter and uses
# `pip --platform manylinux2014_<arch>` to fetch architecture-correct
# wheels for the bundled site-packages.
#
# Output: dist/tbdtask-<version>-<arch>.AppImage
#
# Required tools on the build host:
#   - bash, curl, tar
#   - python3 with pip (the host python is only used to drive cross-install;
#     the bundled interpreter is what actually runs at runtime)
#
# Cross-build note: runtime deps are kept pure-python wherever possible.
# The one exception is pydantic-core (FastAPI's validation core), which is
# a Rust extension and requires a manylinux2014_aarch64 wheel from PyPI.
# Those wheels are published, so the cross-arch install completes without
# native toolchains on the build host.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
ARCH="${ARCH:-aarch64}"
PY_VERSION="${PY_VERSION:-3.11.9}"
PBS_RELEASE="${PBS_RELEASE:-20240415}"
APP_NAME="tbdtask"
APP_VERSION="$(grep -E '^version' "$ROOT/pyproject.toml" | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"
DIST="$ROOT/dist"
APPDIR="$DIST/${APP_NAME}.AppDir"

case "$ARCH" in
  x86_64)
    PY_TRIPLE="x86_64-unknown-linux-gnu"
    PIP_PLATFORM="manylinux2014_x86_64"
    AT_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
    ;;
  aarch64)
    PY_TRIPLE="aarch64-unknown-linux-gnu"
    PIP_PLATFORM="manylinux2014_aarch64"
    AT_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-aarch64.AppImage"
    ;;
  *) echo "unsupported ARCH=$ARCH (use aarch64 or x86_64)"; exit 1 ;;
esac

echo "Target architecture: $ARCH"
echo "Python build:        $PY_TRIPLE"
echo "Wheel platform:      $PIP_PLATFORM"

mkdir -p "$DIST"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/share/${APP_NAME}"

# 1. Portable Python interpreter for the target arch ------------------------
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/cpython-${PY_VERSION}+${PBS_RELEASE}-${PY_TRIPLE}-install_only.tar.gz"
echo "[1/4] Fetching portable Python ($PY_VERSION) for $PY_TRIPLE..."
PY_ARCHIVE="$DIST/python-${PY_VERSION}-${ARCH}.tar.gz"
if [[ ! -f "$PY_ARCHIVE" ]]; then
  curl -fL -o "$PY_ARCHIVE" "$PBS_URL"
fi
tar -xzf "$PY_ARCHIVE" -C "$APPDIR/usr"

# 2. Install runtime deps as architecture-correct wheels --------------------
echo "[2/4] Installing runtime dependencies as $PIP_PLATFORM wheels..."
SITE_DIR="$APPDIR/usr/share/${APP_NAME}/site-packages"
mkdir -p "$SITE_DIR"
# We use the host's python to drive pip with --target so the install does
# not run any code from the wheels. For pure-python packages this works
# regardless of arch. For pydantic-core we explicitly request the
# manylinux2014_<arch> wheel.
python3 -m pip install \
  --no-cache-dir \
  --target "$SITE_DIR" \
  --platform "$PIP_PLATFORM" \
  --python-version "${PY_VERSION%.*}" \
  --implementation cp --abi "cp${PY_VERSION//./}" \
  --only-binary=:all: \
  --upgrade \
  fastapi 'uvicorn>=0.27' sqlalchemy jinja2 python-multipart pydantic pydantic-core

# Bundle the app source.
cp -R "$ROOT/app" "$APPDIR/usr/share/${APP_NAME}/app"

# 3. AppRun launcher and metadata -----------------------------------------
echo "[3/4] Writing AppRun launcher..."
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
APP_BASE="$HERE/usr/share/tbdtask"
export PYTHONPATH="$APP_BASE:$APP_BASE/site-packages:$PYTHONPATH"
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
Categories=Office;Utility;
EOF

# Use a real icon if one is provided; otherwise emit a 1x1 placeholder.
if [[ -f "$ROOT/resources/tbdtask.png" ]]; then
  cp "$ROOT/resources/tbdtask.png" "$APPDIR/${APP_NAME}.png"
else
  printf '\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xfc\xff\xff?\x00\x05\xfe\x02\xfe\xa3\x35\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82' > "$APPDIR/${APP_NAME}.png"
fi

# 4. Assemble AppImage ----------------------------------------------------
echo "[4/4] Running appimagetool..."
APPIMAGETOOL="$DIST/appimagetool-$ARCH"
if [[ ! -x "$APPIMAGETOOL" ]]; then
  curl -fL -o "$APPIMAGETOOL" "$AT_URL"
  chmod +x "$APPIMAGETOOL"
fi

OUT="$DIST/${APP_NAME}-${APP_VERSION}-${ARCH}.AppImage"
# When cross-building (host != target), appimagetool needs --runtime-file
# pointing at a target-arch runtime. The continuous build of appimagetool
# embeds host-arch by default; to keep cross-build simple we tell it which
# arch we want via env, which it honours when packing.
ARCH="$ARCH" "$APPIMAGETOOL" --no-appstream "$APPDIR" "$OUT"

echo
echo "Built: $OUT"
echo "Copy to the Pi and: chmod +x $(basename "$OUT") && ./$(basename "$OUT")"
