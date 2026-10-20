#!/usr/bin/env python3
# Copyright (C) 2026 G. Austin Servantes
# SPDX-License-Identifier: GPL-3.0-or-later
"""End-to-end check against a real, isolated ClipKeep (~20 s, needs a desktop).

Starts ClipKeep with its own temporary data folder and app ID, then acts like
an app that copies formatted text and an image, and checks that ClipKeep
stores them and gives them back byte-for-byte.

    python3 tests/roundtrip.py

Note: a normal ClipKeep that is already running will also record this test's
copies. Quit it first (`clipkeep --quit`) or delete the test items afterwards.
"""
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib

os.environ["GDK_BACKEND"] = "x11"
import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkX11", "3.0")
from gi.repository import Gdk, GdkX11, GLib, Gtk  # noqa: E402

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "clipkeep.py")
HTML = '<meta charset="utf-8"><b>Bold</b>, <i>italic</i>, <span style="color:red">red</span> — ünïcode'.encode()
PLAIN = "Bold, italic, red — ünïcode".encode()
failures = []


def png(w, h, rgb):
    raw = b"".join(b"\0" + bytes(rgb) * w for _ in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))  # noqa: E731
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def pump(seconds):
    """Run our main loop so ClipKeep can fetch what we offer."""
    end = time.time() + seconds
    while time.time() < end:
        Gtk.main_iteration_do(False)
        time.sleep(0.01)


class FakeApp:
    """Owns the clipboard with the given {target: bytes}, like a real app."""
    def __init__(self):
        self.w = Gtk.Invisible()
        self.w.realize()
        self.data = {}
        self.w.connect("selection-get", lambda _w, sel, i, t: sel.set(
            sel.get_target(), 8, self.data[sel.get_target().name()]))

    def copy(self, data):
        self.data = data
        sel = Gdk.SELECTION_CLIPBOARD
        Gtk.selection_clear_targets(self.w, sel)
        for i, t in enumerate(data):
            Gtk.selection_add_target(self.w, sel, Gdk.Atom.intern(t, False), i)
        Gtk.selection_owner_set(self.w, sel, GdkX11.x11_get_server_time(self.w.get_window()))
        pump(1.2)


def read_clipboard(target):
    d = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_for_contents(Gdk.Atom.intern(target, False))
    return d.get_data() if d else None


def main():
    tmp = tempfile.mkdtemp(prefix="clipkeep-test-")
    env = dict(os.environ, CLIPKEEP_HOME=tmp, CLIPKEEP_APP_ID="io.github.gaservantes.ClipKeepTest")

    def ck(*args):
        return subprocess.run([sys.executable, APP, *args], env=env, capture_output=True,
                              text=True, timeout=15).stdout

    def listing():
        return [ln.split("\t") for ln in ck("--list").splitlines() if "\t" in ln]

    proc = subprocess.Popen([sys.executable, APP], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        time.sleep(2)
        app = FakeApp()

        app.copy({"text/html": HTML, "text/plain;charset=utf-8": PLAIN, "UTF8_STRING": PLAIN})
        rows = listing()
        check("formatted copy recorded", rows and rows[0][2].startswith("Formatted"), rows)
        rich_id = rows[0][0] if rows else "0"

        image = png(64, 40, (21, 128, 61))
        app.copy({"image/png": image})
        rows = listing()
        check("image copy recorded", rows and rows[0][2] == "Image", rows)
        img_id = rows[0][0] if rows else "0"

        app.copy({"UTF8_STRING": b"something else"})
        ck("--select", rich_id)
        check("restored HTML is byte-identical", read_clipboard("text/html") == HTML)
        check("restored plain text is byte-identical", read_clipboard("UTF8_STRING") == PLAIN)
        check("restoring didn't add a duplicate", len(listing()) == 3)

        ck("--select", img_id)
        check("restored image is byte-identical", read_clipboard("image/png") == image)

        ck("--pin", rich_id)
        ck("--clear")
        rows = listing()
        check("clear keeps pinned items", [r[0] for r in rows] == [rich_id], rows)

        ck("--toggle")
        app.copy({"UTF8_STRING": b"copied while paused"})
        check("paused tracking records nothing", len(listing()) == 1)
        ck("--toggle")

        mode = os.stat(os.path.join(tmp, "history.db")).st_mode & 0o777
        check("history file is private (600)", mode == 0o600, oct(mode))
    finally:
        ck("--quit")
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        err = proc.stderr.read().decode().strip()
        check("no errors from ClipKeep", "Traceback" not in err, err[-500:])
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PASSED" if not failures else f"{len(failures)} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
