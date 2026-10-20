# Copyright (C) 2026 G. Austin Servantes
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for ClipKeep's clipboard logic (no running app needed).

    python3 -m unittest discover -s tests
"""
import importlib.util
import os
import sqlite3
import stat
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("clipkeep", os.path.join(HERE, "..", "clipkeep.py"))
ck = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ck)


class TargetSelection(unittest.TestCase):
    def test_meta_targets_dropped_and_rich_text_first(self):
        got = ck.select_targets(["TARGETS", "TIMESTAMP", "UTF8_STRING", "text/html",
                                 "chromium/x-source-url", "SAVE_TARGETS"])
        self.assertEqual(got, ["text/html", "UTF8_STRING", "chromium/x-source-url"])

    def test_only_png_kept_among_image_conversions(self):
        got = ck.select_targets(["image/jpeg", "image/png", "image/bmp", "application/x-qt-image"])
        self.assertEqual(got, ["image/png"])

    def test_first_image_kept_when_no_png(self):
        self.assertEqual(ck.select_targets(["image/jpeg", "image/bmp"]), ["image/jpeg"])


class Describe(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(ck.describe({"UTF8_STRING": b"hi"})[:2], ("text", "Plain text"))
        self.assertEqual(ck.describe({"text/html": b"<b>hi</b>", "UTF8_STRING": b"hi"})[0], "rich")
        self.assertEqual(ck.describe({"image/png": b"\x89PNG"})[0], "image")
        self.assertEqual(ck.describe({"text/uri-list": b"file:///tmp/a"})[0], "files")

    def test_empty_and_image_copies_filtered(self):
        webkit_nothing = ck.describe({"org.webkitgtk.WebKit.custom-pasteboard-data": b"\x00\x01"})
        self.assertFalse(ck.worth_keeping(webkit_nothing))
        self.assertFalse(ck.worth_keeping(ck.describe({"UTF8_STRING": b""})))
        self.assertTrue(ck.worth_keeping(ck.describe({"UTF8_STRING": b" "})))
        self.assertTrue(ck.worth_keeping(ck.describe({"text/html": b"<b>hi</b>"})))
        image = ck.describe({"image/png": b"\x89PNG"})
        self.assertTrue(ck.worth_keeping(image))
        self.assertFalse(ck.worth_keeping(image, save_images=False))

    def test_html_only_gets_plain_text(self):
        self.assertEqual(ck.describe({"text/html": b"<p>one</p><p>two</p>"})[2].split(), ["one", "two"])

    def test_utf16_html(self):
        data = "<b>héllo</b>".encode("utf-16")
        self.assertIn("héllo", ck.describe({"text/html": data})[2])


class ContentHash(unittest.TestCase):
    def test_same_text_different_aliases_is_one_item(self):
        a = {"UTF8_STRING": b"hi", "TEXT": b"hi", "STRING": b"hi", "COMPOUND_TEXT": b"hi"}
        b = {"text/plain;charset=utf-8": b"hi", "UTF8_STRING": b"hi"}
        self.assertEqual(ck.content_hash(a), ck.content_hash(b))

    def test_formatting_makes_items_distinct(self):
        bold = {"text/html": b"<b>hi</b>", "UTF8_STRING": b"hi"}
        ital = {"text/html": b"<i>hi</i>", "UTF8_STRING": b"hi"}
        self.assertNotEqual(ck.content_hash(bold), ck.content_hash(ital))
        self.assertNotEqual(ck.content_hash(bold), ck.content_hash({"UTF8_STRING": b"hi"}))


class Labels(unittest.TestCase):
    def strip_mnemonic(self, label):
        # what the GNOME AppIndicator extension does to every menu label
        import re
        return re.sub(r"_([^_])", r"\1", label, count=1)

    def test_menu_text_survives_mnemonic_stripping(self):
        for s in ("plain", "path_with_underscores", "__init__", "a_b_c", "x_"):
            self.assertEqual(self.strip_mnemonic(ck.menu_text(s)), s)

    def test_friendly_formats(self):
        got = ck.friendly_formats(["text/html", "UTF8_STRING", "text/plain", "image/png",
                                   "chromium/x-source-url"])
        self.assertEqual(got, ["formatted text (HTML)", "plain text", "image (PNG)",
                               "app-specific data"])

    def test_desktop_exec_quotes_spaces(self):
        self.assertEqual(ck.desktop_exec("/a/b.py", "--show"), "/a/b.py --show")
        self.assertEqual(ck.desktop_exec("/my apps/c.py"), '"/my apps/c.py"')


class Preview(unittest.TestCase):
    def test_copied_meta_and_scripts_cannot_override_policy(self):
        page = ck.preview_html("rich", {"text/html": (
            b'<meta http-equiv="Content-Security-Policy" content="default-src *">'
            b'<base href="https://evil.example/"><script>alert(1)</script><b>ok</b>')})
        self.assertEqual(page.count("Content-Security-Policy"), 1)
        self.assertNotIn("<script", page)
        self.assertNotIn("<base", page)
        self.assertIn("<b>ok</b>", page)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ck.Store(os.path.join(self.tmp.name, "data", "history.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def add_text(self, s):
        return self.store.add([("UTF8_STRING", "UTF8_STRING", 8, s.encode())], limit=3)

    def test_private_permissions(self):
        mode = stat.S_IMODE(os.stat(self.store.path).st_mode)
        dmode = stat.S_IMODE(os.stat(os.path.dirname(self.store.path)).st_mode)
        self.assertEqual((mode, dmode), (0o600, 0o700))

    def test_limit_spares_pins_and_dedup_moves_to_top(self):
        first = self.add_text("one")
        self.store.set_pinned(first, True)
        for s in ("two", "three", "four", "five"):
            self.add_text(s)
        texts = [r[4] for r in self.store.items()]
        self.assertEqual(texts, ["one", "five", "four", "three"])  # pin + 3 newest
        self.add_text("three")  # copied again
        self.assertEqual([r[4] for r in self.store.items()][1], "three")

    def test_clear_keeps_pins_unless_asked(self):
        keep = self.add_text("keep")
        self.store.set_pinned(keep, True)
        self.add_text("drop")
        self.store.clear()
        self.assertEqual([r[4] for r in self.store.items()], ["keep"])
        self.store.clear(include_pinned=True)
        self.assertEqual(self.store.items(), [])

    def test_preview_loads_only_what_it_shows(self):
        big = b"x" * 1000
        item = self.store.add([("text/html", "text/html", 8, b"<b>hi</b>"),
                               ("UTF8_STRING", "UTF8_STRING", 8, b"hi"),
                               ("application/x-openoffice-embed-source-xml", "x", 8, big)], limit=10)
        targets, data = self.store.preview_formats(item)
        self.assertEqual(len(targets), 3)
        self.assertEqual(set(data), {"text/html", "UTF8_STRING"})

    def test_v1_database_gains_pinned_column(self):
        path = os.path.join(self.tmp.name, "old.db")
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
                   "hash TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, badge TEXT NOT NULL, "
                   "text TEXT NOT NULL, size INTEGER NOT NULL, thumb BLOB)")
        db.execute("INSERT INTO items (ts, hash, kind, badge, text, size) VALUES (1, 'h', 'text', 'Plain text', 'old', 3)")
        db.commit()
        db.close()
        rows = ck.Store(path).items()
        self.assertEqual((rows[0][4], rows[0][7]), ("old", 0))


if __name__ == "__main__":
    unittest.main()
