#!/usr/bin/env bash
# Bundle WebKitGTK + its non-system dependencies into the AppDir so the
# AppImage runs on hosts without webkit2gtk installed.
#
# Usage: tools/bundle_webkit.sh <AppDir> <target-triple>
#   target-triple: aarch64-linux-gnu | x86_64-linux-gnu
#
# What gets bundled:
#   * libwebkit2gtk-4.1 + libjavascriptcoregtk-4.1 + libsoup-3.0 and the
#     full closure of their dependencies, MINUS the AppImage excludelist
#     (libc, libpthread, X11, GL, drm, etc. — those must come from the
#     host because they couple tightly to kernel, drivers, and glibc).
#   * The WebKit helper binaries (WebKitWebProcess, WebKitNetworkProcess,
#     WebKitGPUProcess, injected-bundle) under usr/libexec/.
#   * GIO module data (settings schemas, glib networking) needed for
#     libsoup HTTPS + gsetting lookups.
#
# AppRun is expected to set LD_LIBRARY_PATH and WEBKIT_EXEC_PATH to
# point at these paths.

set -euo pipefail

APPDIR="${1:?missing AppDir}"
TRIPLE="${2:-aarch64-linux-gnu}"

HOST_LIB="/usr/lib/${TRIPLE}"
HOST_WEBKIT_PRIV="${HOST_LIB}/webkit2gtk-4.1"
BUNDLE_LIB="${APPDIR}/usr/lib"
BUNDLE_WEBKIT_PRIV="${APPDIR}/usr/libexec/webkit2gtk-4.1"

if [[ ! -d "$HOST_WEBKIT_PRIV" ]]; then
  echo "tools/bundle_webkit.sh: $HOST_WEBKIT_PRIV not found; install" \
       "libwebkit2gtk-4.1-dev:${TRIPLE##*-} on the build host." >&2
  exit 1
fi

mkdir -p "$BUNDLE_LIB" "$BUNDLE_WEBKIT_PRIV"

# Cross-arch readelf so this works when the build host arch != target.
READELF="readelf"
case "$TRIPLE" in
  aarch64-linux-gnu) command -v aarch64-linux-gnu-readelf >/dev/null && \
    READELF="aarch64-linux-gnu-readelf" ;;
  x86_64-linux-gnu)  command -v x86_64-linux-gnu-readelf >/dev/null && \
    READELF="x86_64-linux-gnu-readelf" ;;
esac

# AppImage excludelist (curated from the upstream excludelist + our
# extras): libraries that must come from the host because they couple
# tightly to kernel/drivers/glibc. Keep this in sync with the AppImage
# project's excludelist when upgrading; the goal is "leaf application
# libs in the bundle, system platform libs from the host".
#
# Notes for non-systemd distros (Void/runit, Devuan/sysvinit, Alpine):
# libsystemd.so.0 is intentionally NOT excluded. WebKit and libdbus link
# it for sd_journal/sd_bus calls. We bundle it (and its libcap dep) so
# the AppImage works on systems without systemd installed; the bundled
# copy still talks to nothing at runtime (no journal socket, no system
# bus dependency — webkit only uses it for soft-fail logging).
EXCLUDE_RE='^(ld-linux|libc|libdl|libpthread|librt|libresolv|libm|libcrypt|libnss_|libBrokenLocale|libanl|libutil)\.so'
EXCLUDE_RE+='|^libG[L]'        # libGL, libGLX, libGLU, libGLdispatch
EXCLUDE_RE+='|^libEGL\.'
EXCLUDE_RE+='|^libgbm\.|^libdrm\.|^libxshmfence'
EXCLUDE_RE+='|^libX'           # X11 family (libX11, libXext, libXi, ...)
EXCLUDE_RE+='|^libxcb|^libxkbcommon|^libxkbfile|^libwayland'
EXCLUDE_RE+='|^libgcc_s\.|^libstdc\+\+\.'
EXCLUDE_RE+='|^libapparmor\.'
EXCLUDE_RE+='|^libudev\.'      # eudev on Void provides this
EXCLUDE_RE+='|^libsoup-2\.'    # libsoup2 — we ship libsoup3

declare -A queued
queue=()

enqueue() {
  local f="$1"
  if [[ -n "${queued[$f]:-}" ]]; then return 0; fi
  if [[ ! -f "$f" ]]; then return 0; fi
  queued[$f]=1
  queue+=("$f")
}

# Seed the queue with the Tauri binary and the webkit helpers — their
# transitive closure defines what we need.
enqueue "${APPDIR}/usr/bin/tbdtask-desktop"
for helper in WebKitWebProcess WebKitNetworkProcess WebKitGPUProcess; do
  enqueue "${HOST_WEBKIT_PRIV}/${helper}"
done
# WebKit also dlopens the injected-bundle plugin at runtime.
if [[ -f "${HOST_WEBKIT_PRIV}/injected-bundle/libwebkit2gtkinjectedbundle.so" ]]; then
  enqueue "${HOST_WEBKIT_PRIV}/injected-bundle/libwebkit2gtkinjectedbundle.so"
fi

while [[ ${#queue[@]} -gt 0 ]]; do
  cur="${queue[0]}"; queue=("${queue[@]:1}")
  for dep in $("$READELF" -d "$cur" 2>/dev/null \
               | awk '/NEEDED/{gsub(/[\[\]]/,"",$5); print $5}'); do
    [[ "$dep" =~ $EXCLUDE_RE ]] && continue
    if [[ -f "${HOST_LIB}/${dep}" ]]; then
      enqueue "${HOST_LIB}/${dep}"
    elif [[ -f "/lib/${TRIPLE}/${dep}" ]]; then
      enqueue "/lib/${TRIPLE}/${dep}"
    fi
  done
done

# Copy libs (resolving symlinks and re-creating the SONAME alias).
echo "  Bundling $((${#queued[@]} - 1)) shared libraries..."
for f in "${!queued[@]}"; do
  case "$f" in
    "${APPDIR}/usr/bin/tbdtask-desktop") continue ;;
    "${HOST_WEBKIT_PRIV}"/*)
      # Preserve subdirs under webkit2gtk-4.1/ (notably injected-bundle/,
      # which WebKit dlopens via the patched static path and won't find
      # if we flatten it into the parent dir).
      rel="${f#${HOST_WEBKIT_PRIV}/}"
      install -Dm755 "$f" "${BUNDLE_WEBKIT_PRIV}/${rel}"
      continue ;;
  esac
  base="$(basename "$f")"
  # Copy the real file (dereference symlinks).
  cp -L "$f" "${BUNDLE_LIB}/${base}"
  chmod 644 "${BUNDLE_LIB}/${base}" 2>/dev/null || true
  # If the source was a symlink (e.g. libfoo.so.0 -> libfoo.so.0.21.7),
  # recreate the symlink chain so loaders can find it under either name.
  if [[ -L "$f" ]]; then
    target="$(readlink "$f")"
    target_base="$(basename "$target")"
    if [[ -f "${BUNDLE_LIB}/${target_base}" || "${target_base}" == "${base}" ]]; then
      # We already copied the real file under the symlink's name; create
      # an alias from the target name so both lookups work.
      (cd "${BUNDLE_LIB}" && ln -sf "${base}" "${target_base}" 2>/dev/null || true)
    fi
  fi
done

# Compiled GSettings schemas (needed by libgio at runtime for ``g_settings_*``
# lookups; many GTK widgets touch them implicitly).
if [[ -f "/usr/share/glib-2.0/schemas/gschemas.compiled" ]]; then
  mkdir -p "${APPDIR}/usr/share/glib-2.0/schemas"
  cp "/usr/share/glib-2.0/schemas/gschemas.compiled" \
     "${APPDIR}/usr/share/glib-2.0/schemas/"
fi

# Intentionally NOT bundled:
#   * gio/modules — glib-networking is only needed for HTTPS via libsoup,
#     and our app is localhost-only. Bundling adds risk without benefit.
#   * gdk-pixbuf-2.0/loaders/*.so + loaders.cache — the cache file is
#     populated by gdk-pixbuf-query-loaders at install time on the target
#     host; cross-built deb packages ship empty caches. WebKit decodes
#     <img> tags itself (libwebp/libpng built in), so pixbuf loaders are
#     not required for our content. If we ever ship a GTK menu with
#     custom icons, revisit this.

echo "  Bundle staged at ${BUNDLE_LIB} and ${BUNDLE_WEBKIT_PRIV}."

# Patch the hardcoded helper paths in libwebkit2gtk so it spawns the
# bundled WebKit*Process binaries instead of looking at the Ubuntu
# install path. WEBKIT_EXEC_PATH was removed in webkit2gtk-4.1, so this
# is the only knob left. AppRun is expected to create the symlink
# /tmp/.tbdtask/webkit2gtk-4.1 -> $HERE/usr/libexec/webkit2gtk-4.1 at
# launch time so the patched path resolves.
if [[ -f "${BUNDLE_LIB}/libwebkit2gtk-4.1.so.0" ]]; then
  echo "  Patching baked WebKit helper paths..."
  python3 "$(dirname "$0")/patch_webkit_paths.py" \
    "${BUNDLE_LIB}/libwebkit2gtk-4.1.so.0"
fi
