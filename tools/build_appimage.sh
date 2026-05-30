#!/usr/bin/env bash
# Build a self-contained tbdtask AppImage shipping the native tkinter UI.
#
# WHY THIS BUILDS THE WAY IT DOES
# -------------------------------
# Earlier builds used python-build-standalone (PBS), whose Tk is statically
# linked against a libX11/libxcb that aborts with an xcb sequence-number
# assertion the moment a widget renders (reproducible on Xvfb AND a real
# Xephyr server). The fix is a DYNAMICALLY-linked Tk: a python whose
# _tkinter.so loads bundled shared libtcl/libtk/libX11/libxcb — exactly the
# configuration that works on a normal Linux desktop.
#
# So we assemble the runtime from Debian/Ubuntu packages:
#   * x86_64 (default for local/CI testing): copy the build host's own
#     python3 + tk + their shared-lib closure into the AppDir. Fast and
#     uses the exact files we can test here.
#   * aarch64 (Pi 400): extract the matching arm64 .debs into the AppDir
#     (set ARCH=aarch64; needs `ar`/`tar` and network to fetch the .debs,
#     or point DEB_DIR at a directory of pre-downloaded arm64 .debs).
#
# The app's own runtime deps (SQLAlchemy, Alembic, ReportLab, …) are pip-
# installed into the bundle as architecture-correct wheels.
#
# Output: dist/tbdtask-<version>-<arch>.AppImage

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
ARCH="${ARCH:-x86_64}"
WITH_PDF="${WITH_PDF:-1}"
APP_NAME="tbdtask"
APP_VERSION="$(grep -E '^version' "$ROOT/pyproject.toml" | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"
DIST="$ROOT/dist"
APPDIR="$DIST/${APP_NAME}.AppDir"
PYVER="${PYVER:-3.12}"           # host python series for x86_64 assembly

case "$ARCH" in
  x86_64)  TRIPLE="x86_64-linux-gnu";  PIP_PLATFORM="manylinux2014_x86_64" ;;
  aarch64) TRIPLE="aarch64-linux-gnu"; PIP_PLATFORM="manylinux2014_aarch64" ;;
  *) echo "unsupported ARCH=$ARCH (use x86_64 or aarch64)"; exit 1 ;;
esac

echo "Target architecture: $ARCH ($TRIPLE)"
echo "Bundle PDF export:   $WITH_PDF"

mkdir -p "$DIST"
rm -rf "$APPDIR"
PYHOME="$APPDIR/usr/python"
LIBS="$APPDIR/usr/libs"
mkdir -p "$PYHOME/bin" "$PYHOME/lib" "$LIBS" "$APPDIR/usr/share/${APP_NAME}"

# Shared libraries the Tk stack needs (the _tkinter.so dependency closure).
TK_LIBS=(
  libtk8.6.so libtcl8.6.so libX11.so.6 libXau.so.6 libXdmcp.so.6
  libXext.so.6 libXft.so.2 libXrender.so.1 libXss.so.1 libfontconfig.so.1
  libfreetype.so.6 libxcb.so.1 "libBLT.2.5.so.8.6" libbsd.so.0 libmd.so.0
  libpng16.so.16 libexpat.so.1 libuuid.so.1 libz.so.1
)

copy_lib() {  # find a lib by name under the lib dirs and copy (deref symlink)
  local name="$1" base="${2:-}"
  for d in "${base}/lib/$TRIPLE" "${base}/usr/lib/$TRIPLE" "/lib/$TRIPLE" "/usr/lib/$TRIPLE"; do
    if [[ -e "$d/$name" ]]; then cp -aL "$d/$name" "$LIBS/" 2>/dev/null && return 0; fi
  done
  return 1
}

if [[ "$ARCH" == "x86_64" ]]; then
  echo "[1/3] Assembling python$PYVER + Tk from the build host..."
  cp -a "/usr/bin/python$PYVER" "$PYHOME/bin/python3"
  cp -a "/usr/lib/python$PYVER" "$PYHOME/lib/python$PYVER"
  # libpython
  copy_lib "libpython$PYVER.so.1.0" || cp -aL /usr/lib/$TRIPLE/libpython$PYVER.so.1.0 "$LIBS/"
  # Tk + X shared libs
  for l in "${TK_LIBS[@]}"; do copy_lib "$l" || echo "  warn: $l not found"; done
  # tcl/tk script libraries
  cp -a /usr/share/tcltk/tcl8.6 "$PYHOME/lib/tcl8.6"
  cp -a /usr/share/tcltk/tk8.6 "$PYHOME/lib/tk8.6"
else
  echo "[1/3] Assembling python + Tk from arm64 .debs..."
  # Pull arm64 .debs (pre-downloaded into DEB_DIR, or fetched here).
  WORK="$DIST/arm64-debs"; mkdir -p "$WORK"
  DEB_DIR="${DEB_DIR:-$WORK}"
  if [[ -z "$(ls "$DEB_DIR"/*.deb 2>/dev/null || true)" ]]; then
    echo "  No .debs in $DEB_DIR — fetching from Debian Bookworm (python3.11)..."
    # Minimal set; dependency-complete for a headless Tk runtime.
    # Resolve current filenames from the Bookworm arm64 Packages index rather
    # than hardcoding versions (point releases move: deb12u6 -> u7 -> ...).
    SUITE="${DEBIAN_SUITE:-bookworm}"
    MIRROR="${DEBIAN_MIRROR:-https://deb.debian.org/debian}"
    echo "  Resolving package filenames from $SUITE/arm64 index..."
    curl -fL --retry 3 -o "$WORK/Packages.gz" \
      "$MIRROR/dists/$SUITE/main/binary-arm64/Packages.gz"
    # Packages we need (binary package name -> pulled from the index).
    NEED=(
      libpython3.11-minimal libpython3.11-stdlib python3.11-minimal libpython3.11
      python3-tk libtcl8.6 libtk8.6
      libx11-6 libxau6 libxdmcp6 libxext6 libxft2 libxrender1 libxss1
      libfontconfig1 libfreetype6 libxcb1 libbsd0 libmd0 libpng16-16
      libexpat1 libbrotli1 libgraphite2-3 libharfbuzz0b
    )
    for pkg in "${NEED[@]}"; do
      fn=$(zcat "$WORK/Packages.gz" | awk -v p="$pkg" '
        $1=="Package:" {cur=$2}
        $1=="Filename:" && cur==p {print $2; exit}')
      if [[ -n "$fn" ]]; then
        curl -fL --retry 3 -o "$DEB_DIR/$(basename "$fn")" "$MIRROR/$fn" \
          && echo "    + $(basename "$fn")" || echo "    ! failed: $pkg"
      else
        echo "    ! not in index: $pkg"
      fi
    done
  fi
  EXTRACT="$DIST/arm64-root"; rm -rf "$EXTRACT"; mkdir -p "$EXTRACT"
  for deb in "$DEB_DIR"/*.deb; do
    ( cd "$EXTRACT" && ar x "$deb" && tar -xf data.tar.* && rm -f control.tar.* data.tar.* debian-binary )
  done
  # Lay out like the x86_64 path.
  cp -a "$EXTRACT/usr/bin/python3.11" "$PYHOME/bin/python3" 2>/dev/null || \
    cp -a "$EXTRACT/usr/bin/python3.11" "$PYHOME/bin/python3"
  cp -a "$EXTRACT/usr/lib/python3.11" "$PYHOME/lib/python3.11"
  cp -aL "$EXTRACT/usr/lib/$TRIPLE/libpython3.11.so.1.0" "$LIBS/" 2>/dev/null || true
  for l in "${TK_LIBS[@]}"; do copy_lib "$l" "$EXTRACT" || echo "  warn: $l (arm64) not found"; done
  cp -a "$EXTRACT/usr/share/tcltk/tcl8.6" "$PYHOME/lib/tcl8.6"
  cp -a "$EXTRACT/usr/share/tcltk/tk8.6" "$PYHOME/lib/tk8.6"
  PYVER=3.11
fi

# 2. App runtime deps as architecture-correct wheels ------------------------
echo "[2/3] Installing app dependencies ($PIP_PLATFORM wheels)..."
SITE="$PYHOME/lib/python$PYVER/site-packages"
mkdir -p "$SITE"
PKGS=( sqlalchemy alembic mako markupsafe greenlet typing_extensions )
[[ "$WITH_PDF" == "1" ]] && PKGS+=( reportlab pillow )
PYV_MM="${PYVER}"; ABI="cp${PYVER//./}"
python3 -m pip install --no-cache-dir --target "$SITE" \
  --platform "$PIP_PLATFORM" --python-version "$PYV_MM" \
  --implementation cp --abi "$ABI" --only-binary=:all: --upgrade \
  "${PKGS[@]}"

cp -R "$ROOT/app" "$APPDIR/usr/share/${APP_NAME}/app"
cp -R "$ROOT/alembic" "$APPDIR/usr/share/${APP_NAME}/alembic"
cp "$ROOT/alembic.ini" "$APPDIR/usr/share/${APP_NAME}/alembic.ini"

# 3. AppRun + metadata + assembly ------------------------------------------
echo "[3/3] Writing AppRun + assembling AppImage..."
cat > "$APPDIR/AppRun" <<EOF
#!/bin/sh
HERE="\$(dirname "\$(readlink -f "\$0")")"
APP_BASE="\$HERE/usr/share/tbdtask"
PYHOME="\$HERE/usr/python"
export LD_LIBRARY_PATH="\$HERE/usr/libs:\${LD_LIBRARY_PATH:-}"
export PYTHONHOME="\$PYHOME"
export PYTHONPATH="\$APP_BASE:\$PYHOME/lib/python${PYVER}/site-packages:\${PYTHONPATH:-}"
export TCL_LIBRARY="\$PYHOME/lib/tcl8.6"
export TK_LIBRARY="\$PYHOME/lib/tk8.6"
export TBDTASK_DATA_DIR="\${TBDTASK_DATA_DIR:-\${HOME}/.local/share/tbdtask}"
export TBDTASK_SINGLE_TENANT=1
export TBDTASK_LAUNCHER=1
mkdir -p "\$TBDTASK_DATA_DIR"
exec "\$PYHOME/bin/python3" -m app.tk "\$@"
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

if [[ -f "$ROOT/resources/tbdtask.png" ]]; then
  cp "$ROOT/resources/tbdtask.png" "$APPDIR/${APP_NAME}.png"
else
  printf '\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xfc\xff\xff?\x00\x05\xfe\x02\xfe\xa3\x35\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82' > "$APPDIR/${APP_NAME}.png"
fi

HOST_ARCH="$(uname -m)"
case "$HOST_ARCH" in
  x86_64)  HOST_AT_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage" ;;
  aarch64) HOST_AT_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-aarch64.AppImage" ;;
  *) echo "unsupported build host arch=$HOST_ARCH"; exit 1 ;;
esac
APPIMAGETOOL="$DIST/appimagetool-$HOST_ARCH"
if [[ ! -x "$APPIMAGETOOL" ]]; then
  curl -fL -o "$APPIMAGETOOL" "$HOST_AT_URL"; chmod +x "$APPIMAGETOOL"
fi
RUNTIME="$DIST/runtime-$ARCH"
case "$ARCH" in
  x86_64)  RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-x86_64" ;;
  aarch64) RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-aarch64" ;;
esac
[[ -f "$RUNTIME" ]] || curl -fL -o "$RUNTIME" "$RUNTIME_URL"

OUT="$DIST/${APP_NAME}-${APP_VERSION}-${ARCH}.AppImage"
ARCH="$ARCH" "$APPIMAGETOOL" --no-appstream --runtime-file "$RUNTIME" "$APPDIR" "$OUT"
echo
echo "Built: $OUT"
echo "Run:   chmod +x $(basename "$OUT") && ./$(basename "$OUT")"
