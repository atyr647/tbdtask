# Desktop shell (Tauri)

Native window for the local FastAPI app. The Rust binary in
`src-tauri/` is a thin shim that:

1. Picks a free TCP port on `127.0.0.1`.
2. Spawns the bundled Python interpreter running uvicorn on that port.
3. Polls `/healthz` until the server is ready.
4. Opens a Tauri/WebKitGTK window pointing at the URL.
5. Kills the sidecar when the window closes.

The Python app is unchanged — Tauri is purely the window chrome.

## Runtime dependencies

The AppImage bundles WebKitGTK 4.1 + libjavascriptcoregtk + libsoup3
+ GTK3 and ~90 other non-system shared libs (see
`tools/bundle_webkit.sh`), so end users don't need to install anything
beyond a working desktop (glibc, X11, OpenGL drivers — present on any
Linux with a GUI). The bundle adds ~70MB compressed and lets the
AppImage run on Void/Debian/Arch/etc. without per-distro setup.

To opt out of bundling and ship a smaller AppImage that depends on
the host's webkit2gtk package, build with
`BUNDLE_WEBKIT=0 tools/build_appimage.sh`. AppRun will detect the
missing bundle, look up the host's webkit, and fall back to browser
mode with an install hint if neither is available.

## Build (cross-compile from x86_64 to aarch64)

Prerequisites on the build host:

```sh
rustup target add aarch64-unknown-linux-gnu
sudo apt install gcc-aarch64-linux-gnu
sudo dpkg --add-architecture arm64
# (point apt's arm64 sources at ports.ubuntu.com)
sudo apt install libwebkit2gtk-4.1-dev:arm64 libgtk-3-dev:arm64 \
                 libssl-dev:arm64 librsvg2-dev:arm64
```

Then `tools/build_appimage.sh` does everything: cross-compiles the
binary, downloads the portable Python interpreter, bundles them
together, and emits `dist/tbdtask-<version>-aarch64.AppImage`.

Set `BUILD_TAURI=0` to skip the Rust build and produce the older
browser-mode AppImage.

## Build (native on the Pi)

```sh
sudo xbps-install -S rust cargo webkit2gtk-devel gtk+3-devel pkg-config
cd desktop/src-tauri
cargo build --release
```

The resulting binary at
`target/release/tbdtask-desktop` can be run directly during
development; it auto-discovers the FastAPI source by walking up from
the binary location looking for an `app/main.py`.

## Why bundle WebKitGTK?

Earlier iterations of this app required `xbps-install -S webkit2gtk`
on the Pi as a one-time setup step. We now bundle webkit + its 90-ish
non-system shared libs (libsoup3, gstreamer, glib, gtk3, etc.) and
the WebKit helper processes into the AppImage so a fresh Pi can run
the AppImage with zero install steps. The host still provides glibc,
X11, and GPU drivers — none of which can be safely bundled because
they're coupled to the kernel and hardware.

Bundled (~70MB compressed):
* libwebkit2gtk-4.1, libjavascriptcoregtk-4.1
* libsoup-3.0, glib/gobject/gio, gtk3/gdk
* gstreamer-1.0 + standard plugins (webkit uses these for `<video>`)
* WebKit helper binaries: `WebKitWebProcess`, `WebKitNetworkProcess`,
  `WebKitGPUProcess`, plus the injected-bundle plugin

System-provided (the AppImage excludelist):
* glibc family (`libc`, `libpthread`, `libdl`, `libm`, `ld-linux`, …)
* X11 + xcb + xkbcommon + wayland
* OpenGL / EGL / drm / gbm (graphics drivers)
* libgcc_s, libstdc++ (ABI-coupled to the host compiler)
* systemd + udev + capability libraries
