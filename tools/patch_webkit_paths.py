#!/usr/bin/env python3
"""Binary-patch hardcoded WebKitGTK paths in a bundled libwebkit2gtk.

WebKitGTK 4.1 (2.52+) removed the ``WEBKIT_EXEC_PATH`` env var, so the
path it uses to spawn ``WebKitNetworkProcess`` / ``WebKitWebProcess`` /
``WebKitGPUProcess`` is a fixed string baked into the shared library at
compile time -- on Ubuntu that's ``/usr/lib/aarch64-linux-gnu/webkit2gtk-4.1``.
Inside our AppImage that directory doesn't exist on the host (and
shouldn't, since the user may not have webkit2gtk installed), so the
launch fails with::

    Failed to spawn child process
    "/usr/lib/aarch64-linux-gnu/webkit2gtk-4.1/WebKitNetworkProcess"
    (No such file or directory)

Fix: rewrite both baked strings in place to point at a fixed path under
``/tmp`` that AppRun creates as a symlink to the bundled helper dir
before exec'ing the Tauri binary. Same trick works for the injected-bundle
path which has its own baked entry.

The replacement string must be the same length or shorter than the
original (we're overwriting in place); the patcher pads the trailing
bytes with NULs so the C string terminator still kicks in at the right
spot.

Single-instance assumption: the symlink target is shared across
concurrent launches. The app already binds a TCP port for the localhost
sidecar, so multi-instance was never supported anyway.
"""

from __future__ import annotations

import sys
from pathlib import Path

# (original, replacement) -- replacement must be <= len(original).
PATCHES: list[tuple[bytes, bytes]] = [
    (
        b"/usr/lib/aarch64-linux-gnu/webkit2gtk-4.1\x00",
        b"/tmp/.tbdtask/webkit2gtk-4.1\x00",
    ),
    (
        b"/usr/lib/aarch64-linux-gnu/webkit2gtk-4.1/injected-bundle/\x00",
        b"/tmp/.tbdtask/webkit2gtk-4.1/injected-bundle/\x00",
    ),
]


def patch_file(path: Path) -> None:
    data = bytearray(path.read_bytes())
    patched = 0
    for old, new in PATCHES:
        assert len(new) <= len(old), f"replacement too long: {new!r}"
        padded = new + b"\x00" * (len(old) - len(new))
        idx = data.find(old)
        if idx < 0:
            print(f"  WARN: {old!r} not found in {path.name}", file=sys.stderr)
            continue
        # Refuse to patch if the string appears more than once -- the
        # closure walker has us bundling only one libwebkit so this
        # should never trigger, but bail loudly if it does.
        second = data.find(old, idx + 1)
        if second >= 0:
            raise SystemExit(
                f"{path.name}: refusing to patch -- {old!r} appears at both "
                f"offset {idx} and {second}"
            )
        data[idx : idx + len(padded)] = padded
        patched += 1
    path.write_bytes(bytes(data))
    print(f"  Patched {patched}/{len(PATCHES)} strings in {path.name}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} <libwebkit2gtk-4.1.so.0> [...]", file=sys.stderr)
        sys.exit(2)
    for arg in sys.argv[1:]:
        patch_file(Path(arg))
