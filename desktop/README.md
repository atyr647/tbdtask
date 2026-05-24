# Desktop shell (Tauri)

Native window for the local FastAPI app. The Rust binary in
`src-tauri/` is a thin shim that:

1. Picks a free TCP port on `127.0.0.1`.
2. Spawns the bundled Python interpreter running uvicorn on that port.
3. Polls `/healthz` until the server is ready.
4. Opens a Tauri/WebKitGTK window pointing at the URL.
5. Kills the sidecar when the window closes.

The Python app is unchanged — Tauri is purely the window chrome.

## Runtime dependency

The Tauri binary dynamically links against the host's WebKitGTK 4.1
and GTK3. On Void Linux: `sudo xbps-install -S webkit2gtk`. On
Debian/Ubuntu: `apt install libwebkit2gtk-4.1-0`. AppRun checks for
the library and falls back to opening the system browser if missing,
so the AppImage still works without it — just less native.

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

## Why not bundle WebKitGTK?

It's 100+ shared libraries (~150MB) with helper processes that need
specific filesystem layout, and bundling exposes the AppImage to
glibc-version mismatches with the host. The system webkit2gtk
package is a one-line install and Just Works. We optimize for "small
AppImage, one-time install command" rather than "bigger AppImage,
zero install steps".
