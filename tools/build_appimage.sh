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

# pip wheel ABI tag — needs major+minor only (e.g. cp311 for 3.11.x).
IFS=. read -r PY_MAJOR PY_MINOR _ <<< "$PY_VERSION"
PY_PYVER="${PY_MAJOR}.${PY_MINOR}"
PY_ABI="cp${PY_MAJOR}${PY_MINOR}"

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
  --python-version "$PY_PYVER" \
  --implementation cp --abi "$PY_ABI" \
  --only-binary=:all: \
  --upgrade \
  fastapi 'uvicorn>=0.27' sqlalchemy alembic jinja2 python-multipart pydantic pydantic-core mako \
  'authlib>=1.3' 'httpx>=0.27' 'itsdangerous>=2.2'

# Bundle the app source + alembic migrations.
cp -R "$ROOT/app" "$APPDIR/usr/share/${APP_NAME}/app"
cp -R "$ROOT/alembic" "$APPDIR/usr/share/${APP_NAME}/alembic"
cp "$ROOT/alembic.ini" "$APPDIR/usr/share/${APP_NAME}/alembic.ini"

# 2b. Native Tauri shell (optional but recommended) -----------------------
# When ``BUILD_TAURI=1`` (the default), cross-compile the Rust binary that
# hosts a native WebKitGTK window and spawns the Python sidecar. When set
# to 0 the AppImage falls back to launching the Python process directly,
# which opens the app in the system browser. The Tauri shell needs a
# host with the matching arm64 toolchain + webkit/gtk dev libs; see
# desktop/README.md for details.
BUILD_TAURI="${BUILD_TAURI:-1}"
TAURI_DIR="$ROOT/desktop/src-tauri"
TAURI_TARGET="${ARCH}-unknown-linux-gnu"
TAURI_BIN="$TAURI_DIR/target/$TAURI_TARGET/release/tbdtask-desktop"

if [[ "$BUILD_TAURI" == "1" ]]; then
  echo "[2b/4] Cross-compiling Tauri shell for $ARCH..."
  (
    cd "$TAURI_DIR"
    if [[ "$ARCH" == "aarch64" ]]; then
      export PKG_CONFIG_ALLOW_CROSS=1
      export PKG_CONFIG_PATH=/usr/lib/aarch64-linux-gnu/pkgconfig:/usr/share/pkgconfig
      export PKG_CONFIG_LIBDIR=/usr/lib/aarch64-linux-gnu/pkgconfig:/usr/share/pkgconfig
      export PKG_CONFIG_SYSROOT_DIR=/
    fi
    cargo build --release --target "$TAURI_TARGET"
  )
  install -Dm755 "$TAURI_BIN" "$APPDIR/usr/bin/tbdtask-desktop"

  # 2c. Bundle WebKitGTK + non-system deps so end users don't need a
  # webkit2gtk package on their machine. Set BUNDLE_WEBKIT=0 to opt
  # out and ship a slimmer AppImage that depends on the host's webkit.
  if [[ "${BUNDLE_WEBKIT:-1}" == "1" ]]; then
    echo "[2c/4] Bundling WebKitGTK runtime..."
    bash "$ROOT/tools/bundle_webkit.sh" "$APPDIR" "${ARCH}-linux-gnu"
  fi
fi

# 3. AppRun launcher and metadata -----------------------------------------
echo "[3/4] Writing AppRun launcher..."
cat > "$APPDIR/AppRun" <<EOF
#!/bin/sh
HERE="\$(dirname "\$(readlink -f "\$0")")"
APP_BASE="\$HERE/usr/share/tbdtask"
export TBDTASK_PYTHON="\$HERE/usr/python/bin/python3"
export TBDTASK_APP_DIR="\$APP_BASE"
export TBDTASK_SITE_PACKAGES="\$APP_BASE/site-packages"
export PYTHONPATH="\$APP_BASE:\$APP_BASE/site-packages:\$PYTHONPATH"
export TBDTASK_DATA_DIR="\${TBDTASK_DATA_DIR:-\${HOME}/.local/share/tbdtask}"
# Single-user offline mode: no login providers, no remote access.
export TBDTASK_SINGLE_TENANT=1
export TBDTASK_LAUNCHER=1
mkdir -p "\$TBDTASK_DATA_DIR"

if [ -x "\$HERE/usr/bin/tbdtask-desktop" ]; then
  # Native Tauri path. Prefer bundled WebKitGTK + helpers when present;
  # otherwise fall back to whatever's installed on the host system.
  if [ -f "\$HERE/usr/lib/libwebkit2gtk-4.1.so.0" ]; then
    export LD_LIBRARY_PATH="\$HERE/usr/lib:\${LD_LIBRARY_PATH:-}"
    export WEBKIT_EXEC_PATH="\$HERE/usr/libexec/webkit2gtk-4.1"
    export GSETTINGS_SCHEMA_DIR="\$HERE/usr/share/glib-2.0/schemas"
    # Disable WebKit's Bubblewrap sandbox: it can't enter the AppImage
    # FUSE mount as a child namespace. The app is a local trusted tool
    # binding to 127.0.0.1, so this is acceptable here.
    export WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1
    # If a per-pixbuf-loader cache was generated at build time, point at
    # it; otherwise let gdk-pixbuf use its built-in fallbacks (fine for
    # our content since WebKit decodes <img> data itself).
    if [ -s "\$HERE/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache" ]; then
      export GDK_PIXBUF_MODULE_FILE="\$HERE/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache"
    fi
    exec "\$HERE/usr/bin/tbdtask-desktop" "\$@"
  fi
  if ldconfig -p 2>/dev/null | grep -q 'libwebkit2gtk-4.1\.so\.0'; then
    exec "\$HERE/usr/bin/tbdtask-desktop" "\$@"
  fi
  echo "tbdtask: libwebkit2gtk-4.1 not found; falling back to browser mode." >&2
  echo "  Install it on Void Linux with: sudo xbps-install -S webkit2gtk" >&2
fi
exec "\$TBDTASK_PYTHON" -m app.main "\$@"
EOF
chmod +x "$APPDIR/AppRun"

cat > "$APPDIR/${APP_NAME}.desktop" <<EOF
[Desktop Entry]
Name=Worklist Tracker
Comment=Offline weekly worklist and personnel tracker
Exec=AppRun
Icon=tbdtask
Type=Application
Categories=Office;
StartupNotify=true
Terminal=false
EOF

# Use a real icon if one is provided; otherwise emit a 1x1 placeholder.
if [[ -f "$ROOT/resources/tbdtask.png" ]]; then
  cp "$ROOT/resources/tbdtask.png" "$APPDIR/${APP_NAME}.png"
else
  printf '\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xfc\xff\xff?\x00\x05\xfe\x02\xfe\xa3\x35\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82' > "$APPDIR/${APP_NAME}.png"
fi

# 4. Assemble AppImage ----------------------------------------------------
echo "[4/4] Running appimagetool..."

# appimagetool runs on the *build* host; download the host-arch binary.
HOST_ARCH="$(uname -m)"
case "$HOST_ARCH" in
  x86_64)  HOST_AT_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage" ;;
  aarch64) HOST_AT_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-aarch64.AppImage" ;;
  *) echo "unsupported build host arch=$HOST_ARCH"; exit 1 ;;
esac
APPIMAGETOOL="$DIST/appimagetool-$HOST_ARCH"
if [[ ! -x "$APPIMAGETOOL" ]]; then
  curl -fL -o "$APPIMAGETOOL" "$HOST_AT_URL"
  chmod +x "$APPIMAGETOOL"
fi

# The runtime is the small binary embedded at the head of every AppImage;
# it mounts the SquashFS payload at launch. The runtime arch determines
# which CPU the resulting AppImage will run on, so for a cross-build we
# fetch a target-arch runtime explicitly.
RUNTIME="$DIST/runtime-$ARCH"
case "$ARCH" in
  x86_64)  RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-x86_64" ;;
  aarch64) RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-aarch64" ;;
esac
if [[ ! -f "$RUNTIME" ]]; then
  curl -fL -o "$RUNTIME" "$RUNTIME_URL"
fi

OUT="$DIST/${APP_NAME}-${APP_VERSION}-${ARCH}.AppImage"
ARCH="$ARCH" "$APPIMAGETOOL" --no-appstream --runtime-file "$RUNTIME" "$APPDIR" "$OUT"

echo
echo "Built: $OUT"
echo "Copy to the Pi and: chmod +x $(basename "$OUT") && ./$(basename "$OUT")"
