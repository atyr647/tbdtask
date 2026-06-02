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

This binary is cross-built. The build verifies that the **complete
`_tkinter` shared-library closure is bundled** (the first Pi attempt
crashed because Debian's `_tkinter` needs `libBLT.2.5.so.8.6`, which was
missing — that's fixed, and the build now fails loudly if any lib in the
chain is absent). It still has not been launched on real Pi hardware in
CI — only the x86_64 sibling build was confirmed to open a window — so if
anything misbehaves, capture the terminal output and file an issue.

It needs a graphical session: Raspberry Pi OS **Lite has no display
server**, so use the Desktop image (or an X session). The bundled libs
cover the Tk/X stack; only ubiquitous system libraries (libc, libz,
libssl, …) are expected from the OS.

## Rebuilding

```sh
ARCH=aarch64 tools/build_appimage.sh      # Pi 400
ARCH=x86_64  tools/build_appimage.sh      # desktop testing
```

See `tools/build_appimage.sh` for why the build assembles a
dynamically-linked Tk runtime (the short version: bundled static Tk from
python-build-standalone crashes on widget render, on real X servers too).
