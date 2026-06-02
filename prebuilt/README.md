# Prebuilt AppImage (Raspberry Pi 400)

`tbdtask-0.1.0-aarch64.AppImage` is the **native tkinter** Worklist
Tracker, built for the Pi 400 (aarch64). It bundles its own Python +
Tcl/Tk + the app, so there's nothing to install — no browser, no
WebKitGTK, no system packages.

## Run it

```sh
chmod +x tbdtask-0.1.0-aarch64.AppImage
./tbdtask-0.1.0-aarch64.AppImage
```

It opens the native window and keeps its data in
`~/.local/share/tbdtask/`. Override the data location with
`TBDTASK_DATA_DIR`.

Verify the download against `SHA256SUMS.txt`:

```sh
sha256sum -c SHA256SUMS.txt
```

## Heads-up

This binary was cross-built and its structure verified (ARM64 Python +
dynamically-linked Tk), but it has **not yet been launched on real Pi
hardware** — only the x86_64 sibling build was confirmed to open a window
in CI. If the window doesn't appear, capture the terminal output and file
an issue; the most likely culprit would be a missing X library on a
stripped-down OS image (the AppImage bundles the Tk/X closure, but a
headless Raspberry Pi OS Lite has no display server at all — you need the
Desktop image or an X session).

## Rebuilding

```sh
ARCH=aarch64 tools/build_appimage.sh      # Pi 400
ARCH=x86_64  tools/build_appimage.sh      # desktop testing
```

See `tools/build_appimage.sh` for why the build assembles a
dynamically-linked Tk runtime (the short version: bundled static Tk from
python-build-standalone crashes on widget render, on real X servers too).
