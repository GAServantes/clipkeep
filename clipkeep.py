#!/usr/bin/env python3
# Copyright (C) 2026 G. Austin Servantes
# SPDX-License-Identifier: GPL-3.0-or-later
"""ClipKeep - every copy, kept. A clipboard history manager for GNOME.

Every format an application offers on the clipboard (HTML, RTF, plain text,
PNG, file lists, app-specific formats...) is stored byte-for-byte, and all of
them are offered again when an item is restored, so formatted text pastes back
formatted.

Runs through XWayland (GDK_BACKEND=x11) because GNOME's Wayland compositor
does not let background apps watch the clipboard; the X11 bridge does.

Command line (forwarded to the running instance):
  clipkeep              start (tray icon) - or show the window if running
  clipkeep --show       show the history window
  clipkeep --settings   show the settings window
  clipkeep --toggle     pause / resume tracking
  clipkeep --quit       quit the running instance
  clipkeep --list       print the history (id, pin, formats, preview)
  clipkeep --select ID  put item ID back on the clipboard
  clipkeep --pin ID     pin / unpin item ID
  clipkeep --delete ID  remove item ID
  clipkeep --clear      remove all unpinned items

Run on their own, without starting the app (used by install.sh):
  clipkeep --version
  clipkeep --hotkey-default   set Ctrl+Alt+V unless a shortcut is already set
  clipkeep --hotkey-remove    remove ClipKeep's GNOME shortcut
"""
import os

os.environ["GDK_BACKEND"] = "x11"

import base64
import hashlib
import html
import json
import re
import shlex
import sqlite3
import sys
import time
import urllib.parse

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkX11", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
from gi.repository import Gdk, GdkPixbuf, GdkX11, Gio, GLib, Gtk  # noqa: E402

try:
    gi.require_version("WebKit2", "4.1")
    from gi.repository import WebKit2  # noqa: E402
except (ValueError, ImportError):
    WebKit2 = None

VERSION = "2.1.0"
APP_ID = os.environ.get("CLIPKEEP_APP_ID", "io.github.gaservantes.ClipKeep")
HERE = os.path.dirname(os.path.realpath(__file__))
SCRIPT = os.path.join(HERE, "clipkeep.py")
ICON_DIR = os.path.join(HERE, "icons")
DATA_DIR = os.environ.get(
    "CLIPKEEP_HOME", os.path.join(GLib.get_user_data_dir(), "clipkeep"))
AUTOSTART_FILE = os.path.join(GLib.get_user_config_dir(), "autostart", "clipkeep.desktop")
MAX_TARGET_BYTES = 32 * 1024 * 1024
MAX_ITEM_BYTES = 64 * 1024 * 1024

DEFAULTS = {
    "tracking": True,
    "max_items": 100,       # unpinned items kept
    "menu_recent": 10,      # recent items listed in the tray menu
    "save_images": True,    # record image-only copies (screenshots, pictures)
    "persist": True,        # keep unpinned history across restarts
    "preview": True,        # show the preview panel in the window
    "close_after_copy": True,
    "win_width": 0,         # remembered window size / divider (0 = default)
    "win_height": 0,
    "pane": 0,
}

# GNOME custom keybinding that opens the window.
MEDIA_KEYS = "org.gnome.settings-daemon.plugins.media-keys"
KEYBINDING_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/clipkeep/"
DEFAULT_HOTKEY = "<Control><Alt>v"
KEYBINDING_SCHEMAS = [
    "org.gnome.desktop.wm.keybindings", "org.gnome.shell.keybindings",
    "org.gnome.mutter.keybindings", "org.gnome.mutter.wayland.keybindings",
    MEDIA_KEYS,
]

# Clipboard targets that describe the selection itself rather than content.
META_TARGETS = {
    "TARGETS", "TIMESTAMP", "MULTIPLE", "SAVE_TARGETS", "DELETE",
    "INSERT_SELECTION", "INSERT_PROPERTY", "CLIPBOARD_MANAGER",
    "PIXMAP", "BITMAP", "DRAWABLE", "COLORMAP",
}
# Present when a password manager copies a secret - never record those.
SECRET_HINTS = {"x-kde-passwordManagerHint"}
PLAIN_TARGETS = ["text/plain;charset=utf-8", "UTF8_STRING", "text/plain",
                 "STRING", "TEXT"]
RICH_TARGETS = ["text/html", "text/rtf", "text/richtext", "application/rtf",
                "application/x-qt-richtext"]
FILE_TARGETS = ["x-special/gnome-copied-files", "text/uri-list"]


def target_priority(name):
    """Order in which targets are fetched: content that matters most first,
    so a size cap never drops the plain/rich text."""
    if name in RICH_TARGETS:
        return 0
    if name in PLAIN_TARGETS:
        return 1
    if name in FILE_TARGETS or name.startswith("image/"):
        return 2
    return 3


def select_targets(names):
    """Filter the advertised targets down to the ones worth storing."""
    names = [n for n in names if n and n not in META_TARGETS]
    images = [n for n in names if n.startswith("image/")]
    keep_image = "image/png" if "image/png" in images else (images[:1] or [None])[0]
    out = [n for n in names
           if (not n.startswith("image/") or n == keep_image)
           and n != "application/x-qt-image"]
    return sorted(dict.fromkeys(out), key=target_priority)


def decode_text(data):
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    return data.decode("utf-8", "replace")


def html_to_text(markup):
    markup = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d)>", "\n", markup)
    return html.unescape(re.sub(r"<[^>]+>", "", markup))


def describe(formats):
    """-> (kind, badge, full plain text) for a {target: data} dict."""
    text = ""
    for t in PLAIN_TARGETS:
        if formats.get(t):
            text = decode_text(formats[t])
            break
    if not text and formats.get("text/html"):
        text = html_to_text(decode_text(formats["text/html"]))
    text = text.replace("\x00", "")
    badges = []
    if "text/html" in formats:
        badges.append("HTML")
    if any(t in formats for t in ("text/rtf", "text/richtext", "application/rtf")):
        badges.append("RTF")
    if any(t.startswith("application/x-openoffice") for t in formats):
        badges.append("Office")
    if any(t in formats for t in FILE_TARGETS) and not badges:
        return "files", "Files", text
    if any(t.startswith("image/") for t in formats) and not text.strip():
        return "image", "Image", ""
    if badges:
        return "rich", "Formatted (" + ", ".join(badges) + ")", text
    return "text", "Plain text", text


def content_hash(formats, text=None):
    """Identity of the content itself, so the same text copied from apps that
    advertise different sets of plain-text aliases is still one item."""
    keys = [t for t in formats
            if t in RICH_TARGETS or t in FILE_TARGETS or t.startswith("image/")]
    if text is None:
        text = describe(formats)[2]
    if not keys and not text:
        keys = list(formats)
    h = hashlib.sha256(b"text\0" + text.encode("utf-8", "replace") + b"\0")
    for t in sorted(keys):
        h.update(t.encode() + b"\0" + formats[t] + b"\0")
    return h.hexdigest()


def friendly_formats(targets):
    """Plain-language names for stored targets, e.g. 'formatted text (HTML)'."""
    names = []
    for t in targets:
        if t == "text/html":
            n = "formatted text (HTML)"
        elif t in RICH_TARGETS:
            n = "formatted text (RTF)"
        elif t.startswith("application/x-openoffice"):
            n = "LibreOffice data"
        elif t in PLAIN_TARGETS or t == "COMPOUND_TEXT":
            n = "plain text"
        elif t.startswith("image/"):
            n = "image (" + t[6:].upper() + ")"
        elif t in FILE_TARGETS:
            n = "file list"
        else:
            n = "app-specific data"
        if n not in names:
            names.append(n)
    return names


def one_line(text, limit=60):
    s = " ".join(text.split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def menu_text(label):
    """Tray menu labels are exported as mnemonic text and the GNOME
    AppIndicator extension strips only the *first* '_' followed by another
    character. Used with use_underline=True (so libdbusmenu doesn't double
    the underscores), one throwaway leading '_' makes any label - even
    snake_case_text - display exactly."""
    return "_" + label if "_" in label else label


def time_ago(ts):
    d = max(0, time.time() - ts)
    if d < 45:
        return "just now"
    if d < 3600:
        return f"{int(d // 60) or 1} min ago"
    if d < 86400:
        return f"{int(d // 3600)} h ago"
    return time.strftime("%b %d, %H:%M", time.localtime(ts))


def human_size(n):
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def private_dir(path):
    """Clipboard history can hold anything you copied: owner-only access."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


class Store:
    # Pinned items have pinned = time they were pinned (0 = not pinned); they
    # sort first, in the order they were pinned, and never count toward or get
    # removed by the history limit.
    COLUMNS = "id, ts, kind, badge, text, size, thumb, pinned"

    def __init__(self, path):
        private_dir(os.path.dirname(path))
        self.path = path
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                hash TEXT UNIQUE NOT NULL,
                kind TEXT NOT NULL,
                badge TEXT NOT NULL,
                text TEXT NOT NULL,
                size INTEGER NOT NULL,
                thumb BLOB);
            CREATE TABLE IF NOT EXISTS formats (
                item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                pos INTEGER NOT NULL,
                target TEXT NOT NULL,
                dtype TEXT NOT NULL,
                fmt INTEGER NOT NULL,
                data BLOB NOT NULL);
            CREATE INDEX IF NOT EXISTS formats_item ON formats(item_id);
        """)
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(items)")]
        if "pinned" not in cols:
            self.db.execute("ALTER TABLE items ADD COLUMN pinned REAL NOT NULL DEFAULT 0")
        self.db.commit()
        # Deleted items leave free pages behind; give the space back once a
        # good share of the file is empty.
        pages = self.db.execute("PRAGMA page_count").fetchone()[0]
        free = self.db.execute("PRAGMA freelist_count").fetchone()[0]
        if free > 256 and free * 4 > pages:
            self.db.execute("VACUUM")

    def add(self, entries, limit, info=None):
        """entries: list of (target, dtype, fmt, data); info: describe() result
        if the caller already has it. Returns item id."""
        formats = {t: d for t, _, _, d in entries}
        kind, badge, text = info or describe(formats)
        digest = content_hash(formats, text)
        row = self.db.execute("SELECT id FROM items WHERE hash=?", (digest,)).fetchone()
        if row:  # same content copied again: just move it to the top
            self.touch(row[0])
            return row[0]
        png = formats.get("image/png")
        cur = self.db.execute(
            "INSERT INTO items (ts, hash, kind, badge, text, size, thumb) VALUES (?,?,?,?,?,?,?)",
            (time.time(), digest, kind, badge, text[:20000],
             sum(len(e[3]) for e in entries), make_thumb(png) if png else None))
        item_id = cur.lastrowid
        self.db.executemany(
            "INSERT INTO formats (item_id, pos, target, dtype, fmt, data) VALUES (?,?,?,?,?,?)",
            [(item_id, i, t, dt, f, d) for i, (t, dt, f, d) in enumerate(entries)])
        self.prune(limit)
        return item_id

    def prune(self, limit):
        self.db.execute(
            "DELETE FROM items WHERE pinned = 0 AND id NOT IN "
            "(SELECT id FROM items WHERE pinned = 0 ORDER BY ts DESC LIMIT ?)", (limit,))
        self.db.commit()

    def touch(self, item_id):
        self.db.execute("UPDATE items SET ts=? WHERE id=?", (time.time(), item_id))
        self.db.commit()

    def set_pinned(self, item_id, pinned):
        self.db.execute("UPDATE items SET pinned=? WHERE id=?",
                        (time.time() if pinned else 0, item_id))
        self.db.commit()

    def items(self):
        return self.db.execute(
            f"SELECT {self.COLUMNS} FROM items "
            "ORDER BY pinned > 0 DESC, CASE WHEN pinned > 0 THEN pinned END, ts DESC").fetchall()

    def item(self, item_id):
        return self.db.execute(
            f"SELECT {self.COLUMNS} FROM items WHERE id=?", (item_id,)).fetchone()

    def pinned_count(self):
        return self.db.execute("SELECT COUNT(*) FROM items WHERE pinned > 0").fetchone()[0]

    def formats(self, item_id):
        return self.db.execute(
            "SELECT target, dtype, fmt, data FROM formats WHERE item_id=? ORDER BY pos",
            (item_id,)).fetchall()

    def preview_formats(self, item_id):
        """-> (all target names, {target: data} for only what the preview
        shows), so hovering a 30 MB office copy doesn't load all of it."""
        targets = [r[0] for r in self.db.execute(
            "SELECT target FROM formats WHERE item_id=? ORDER BY pos", (item_id,))]
        wanted = []
        for group in (["text/html"], PLAIN_TARGETS, FILE_TARGETS,
                      [t for t in targets if t.startswith("image/")]):
            hit = next((t for t in group if t in targets), None)
            if hit:
                wanted.append(hit)
        if not wanted:
            return targets, {}
        marks = ",".join("?" * len(wanted))
        rows = self.db.execute(
            f"SELECT target, data FROM formats WHERE item_id=? AND target IN ({marks})",
            (item_id, *wanted))
        return targets, {t: bytes(d) for t, d in rows}

    def disk_usage(self):
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def delete(self, item_id):
        self.db.execute("DELETE FROM items WHERE id=?", (item_id,))
        self.db.commit()

    def clear(self, include_pinned=False):
        self.db.execute("DELETE FROM items" + ("" if include_pinned else " WHERE pinned = 0"))
        self.db.commit()
        self.db.execute("VACUUM")


def make_thumb(png, size=96):
    try:
        loader = GdkPixbuf.PixbufLoader.new_with_type("png")
        loader.write(png)
        loader.close()
        pb = loader.get_pixbuf()
        scale = min(size / pb.get_width(), size / pb.get_height(), 1.0)
        pb = pb.scale_simple(max(1, int(pb.get_width() * scale)),
                             max(1, int(pb.get_height() * scale)),
                             GdkPixbuf.InterpType.BILINEAR)
        ok, buf = pb.save_to_bufferv("png", [], [])
        return bytes(buf) if ok else None
    except GLib.Error:
        return None


def pixbuf_from_png(data):
    loader = GdkPixbuf.PixbufLoader.new_with_type("png")
    loader.write(data)
    loader.close()
    return loader.get_pixbuf()


# ---------- preview rendering ----------
# The preview is a WebKit view with scripts off and a Content-Security-Policy
# that blocks every network load, so copied HTML can't phone home.
PREVIEW_HEAD = """<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:">
<style>
 html { background: #ffffff; color: #000000; }
 body { margin: 14px 16px; font: 15px/1.45 sans-serif; overflow-wrap: anywhere; }
 pre.plain { white-space: pre-wrap; font: 14px/1.45 monospace; margin: 0; }
 img.full { max-width: 100%; height: auto; display: block; margin: auto;
            background: repeating-conic-gradient(#e5e5e5 0 25%, #fff 0 50%) 0 0/16px 16px; }
 .note { font: 12px sans-serif; color: #14532d; background: #ecfdf3; padding: 6px 8px;
         border-radius: 6px; margin-bottom: 10px; }
 .empty { font: 14px sans-serif; color: #6b7280; text-align: center; margin-top: 30%; }
 ul.files { font: 14px monospace; padding-left: 18px; }
</style></head><body>"""
PREVIEW_LIMIT = 400_000  # characters of HTML/text shown in the preview


def strip_html_scaffolding(markup):
    """Drop <head>, scripts and meta tags a copied fragment may carry
    (a copied <meta> CSP or <base> must not override ours)."""
    markup = re.sub(r"(?is)<(head|script|title)\b.*?</\1\s*>", "", markup)
    markup = re.sub(r"(?is)<(meta|base|link)\b[^>]*>", "", markup)
    return re.sub(r"(?is)</?(html|body)\b[^>]*>", "", markup)


def preview_html(kind, formats):
    """Build a self-contained HTML page showing the item as it will paste."""
    body = ""
    if kind == "image":
        img = next((t for t in formats if t.startswith("image/")), None)
        if img:
            b64 = base64.b64encode(formats[img]).decode()
            body = f'<img class="full" src="data:{html.escape(img)};base64,{b64}">'
    elif kind == "files":
        raw = decode_text(formats.get("x-special/gnome-copied-files")
                          or formats.get("text/uri-list") or b"")
        paths = [urllib.parse.unquote(ln[7:]) if ln.startswith("file://") else ln
                 for ln in raw.splitlines() if ln and ln not in ("copy", "cut")]
        body = ('<div class="note">Files: pastes into a file manager as files.</div><ul class="files">'
                + "".join(f"<li>{html.escape(p)}</li>" for p in paths) + "</ul>")
    elif "text/html" in formats:
        body = strip_html_scaffolding(decode_text(formats["text/html"])[:PREVIEW_LIMIT])
    else:
        text = describe(formats)[2][:PREVIEW_LIMIT]
        if kind == "rich":
            body = ('<div class="note">This item\'s formatting is RTF/Office data. It '
                    'pastes formatted; the preview shows the text only.</div>')
        body += f'<pre class="plain">{html.escape(text)}</pre>'
    return PREVIEW_HEAD + body + "</body></html>"


# ---------- keyboard shortcut (GNOME custom keybinding) ----------
def _settings(schema_id, path=None):
    source = Gio.SettingsSchemaSource.get_default()
    if not source or not source.lookup(schema_id, True):
        return None
    return Gio.Settings.new_with_path(schema_id, path) if path else Gio.Settings.new(schema_id)


def get_hotkey():
    s = _settings(MEDIA_KEYS)
    if not s or KEYBINDING_PATH not in s.get_strv("custom-keybindings"):
        return ""
    return _settings(MEDIA_KEYS + ".custom-keybinding", KEYBINDING_PATH).get_string("binding")


def set_hotkey(accel):
    s = _settings(MEDIA_KEYS)
    if not s:
        return
    paths = s.get_strv("custom-keybindings")
    if KEYBINDING_PATH not in paths:
        s.set_strv("custom-keybindings", paths + [KEYBINDING_PATH])
    kb = _settings(MEDIA_KEYS + ".custom-keybinding", KEYBINDING_PATH)
    kb.set_string("name", "ClipKeep")
    kb.set_string("command", shlex.quote(SCRIPT) + " --show")
    kb.set_string("binding", accel)
    Gio.Settings.sync()


def remove_hotkey():
    s = _settings(MEDIA_KEYS)
    if not s:
        return
    s.set_strv("custom-keybindings",
               [p for p in s.get_strv("custom-keybindings") if p != KEYBINDING_PATH])
    kb = _settings(MEDIA_KEYS + ".custom-keybinding", KEYBINDING_PATH)
    for key in ("name", "command", "binding"):
        kb.reset(key)
    Gio.Settings.sync()


def hotkey_label(accel):
    key, mods = Gtk.accelerator_parse(accel or "")
    return Gtk.accelerator_get_label(key, mods) if key else ""


def hotkey_conflict(accel):
    """Name of an existing GNOME shortcut that already uses accel, or None."""
    want = Gtk.accelerator_parse(accel)
    names = [(sid, None) for sid in KEYBINDING_SCHEMAS]
    mk = _settings(MEDIA_KEYS)
    if mk:
        names += [(MEDIA_KEYS + ".custom-keybinding", p)
                  for p in mk.get_strv("custom-keybindings") if p != KEYBINDING_PATH]
    for sid, path in names:
        s = _settings(sid, path)
        if not s:
            continue
        for key in s.props.settings_schema.list_keys():
            v = s.get_value(key)
            vals = [v.get_string()] if v.get_type_string() == "s" else (
                list(v.unpack()) if v.get_type_string() == "as" else [])
            for val in vals:
                if val and Gtk.accelerator_parse(val) == want and want[0]:
                    if path:
                        return "custom shortcut “" + s.get_string("name") + "”"
                    return key.replace("-", " ")
    return None


def desktop_exec(*args):
    """Quote a command for a .desktop Exec= line (paths may contain spaces)."""
    def q(a):
        if re.fullmatch(r"[\w./+-]+", a):
            return a
        return '"' + re.sub(r'(["`$\\])', r"\\\1", a) + '"'
    return " ".join(q(a) for a in args)


def set_autostart(on):
    if on:
        os.makedirs(os.path.dirname(AUTOSTART_FILE), exist_ok=True)
        with open(AUTOSTART_FILE, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=ClipKeep\n"
                    "Comment=Clipboard history\n"
                    f"Exec={desktop_exec(SCRIPT)}\n"
                    "Icon=clipkeep\nTerminal=false\n"
                    "X-GNOME-Autostart-enabled=true\nX-GNOME-Autostart-Delay=3\n")
    elif os.path.exists(AUTOSTART_FILE):
        os.remove(AUTOSTART_FILE)


# Light "keep" flavor: deep castle green as the accent everywhere.
CSS = b"""
@define-color ck_green #15803d;
@define-color ck_green_dark #0b4f26;
.ck-banner { padding: 8px 12px; }
.ck-banner.on { background-color: #dcfce7; }
.ck-banner.off { background-color: #fee2e2; }
.ck-banner.on .ck-state { color: #166534; font-weight: bold; }
.ck-banner.off .ck-state { color: #991b1b; font-weight: bold; }
.ck-meta { font-size: smaller; opacity: 0.65; }
.ck-badge-rich { color: #8b5cf6; font-weight: bold; }
.ck-badge-image { color: #0ea5e9; font-weight: bold; }
row.ck-row:selected { background-color: @ck_green; color: #ffffff; }
row:selected .ck-badge-rich, row:selected .ck-badge-image, row:selected .ck-pinned-star { color: inherit; }
.ck-empty { opacity: 0.6; padding: 40px; }
.ck-section { font-weight: bold; font-size: smaller; opacity: 0.75; padding: 8px 10px 4px 10px; }
.ck-pinned-star { color: #f59e0b; }
row.ck-row { padding: 6px 8px; border-bottom: 1px solid alpha(currentColor, 0.08); }
.ck-preview-meta { padding: 8px 10px; }
.ck-hint { font-size: smaller; opacity: 0.65; }
.ck-toast { font-size: smaller; font-weight: bold; color: #22c55e; }
.ck-settings-desc { font-size: smaller; opacity: 0.65; }
.ck-settings-group { font-weight: bold; color: #22c55e; }
.ck-settings-foot { font-size: smaller; opacity: 0.6; }
.ck-warn { color: #dc2626; }
.ck-window switch:checked { background-color: @ck_green; border-color: @ck_green_dark; }
"""


class ClipKeep(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.window = None
        self.settings_window = None
        self.store = None
        self.capture_gen = 0
        self.debounce_id = 0
        self.serving = {}  # target name -> (dtype, fmt, data) we currently own
        self.config_path = os.path.join(DATA_DIR, "config.json")

    # ---------- lifecycle ----------
    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.hold()  # keep running with no window open
        self.cfg = dict(DEFAULTS, **self.load_config())
        self.store = Store(os.path.join(DATA_DIR, "history.db"))
        if not self.cfg["persist"]:
            self.store.clear(include_pinned=False)
        self.store.prune(self.cfg["max_items"])

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.Window.set_default_icon_from_file(os.path.join(ICON_DIR, "clipkeep-on.svg"))

        self.clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        self.clipboard.connect("owner-change", self.on_owner_change)

        # Invisible widget that owns the clipboard when we restore an item.
        self.owner = Gtk.Invisible()
        self.owner.realize()
        self.owner.connect("selection-get", self.on_selection_get)
        self.owner.connect("selection-clear-event", self.on_selection_clear)

        self.indicator = AppIndicator.Indicator.new(
            APP_ID, "clipkeep-on", AppIndicator.IndicatorCategory.APPLICATION_STATUS)
        self.indicator.set_icon_theme_path(ICON_DIR)
        self.indicator.set_title("ClipKeep")
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.menu = Gtk.Menu()
        self.indicator.set_menu(self.menu)
        self.refresh()

    @property
    def tracking(self):
        return self.cfg["tracking"]

    def do_command_line(self, cmd):
        args = cmd.get_arguments()[1:]
        first_run = not getattr(self, "_started", False)
        self._started = True
        out = []
        try:
            if not args:
                if not first_run:
                    self.show_window()
            elif args[0] == "--show":
                self.show_window()
            elif args[0] == "--settings":
                self.show_settings()
            elif args[0] == "--toggle":
                self.set_tracking(not self.tracking)
                out.append("tracking " + ("on" if self.tracking else "off"))
            elif args[0] == "--quit":
                self.quit()
            elif args[0] == "--list":
                for row in self.store.items():
                    fm = ",".join(f[0] for f in self.store.formats(row[0]))
                    pin = "pinned" if row[7] else "-"
                    out.append(f"{row[0]}\t{pin}\t{row[3]}\t{fm}\t{one_line(row[4], 50)}")
                out.append("tracking " + ("on" if self.tracking else "off"))
            elif args[0] == "--select":
                self.restore(int(args[1]))
            elif args[0] == "--pin":
                self.toggle_pin(int(args[1]))
            elif args[0] == "--delete":
                self.delete_item(int(args[1]))
            elif args[0] == "--clear":
                self.clear_all()
            else:
                out.append(__doc__)
        except (IndexError, ValueError):
            out.append(__doc__)
        if out:
            cmd.print_literal("\n".join(out) + "\n")
        return 0

    def load_config(self):
        try:
            os.chmod(self.config_path, 0o600)  # files from older versions were 664
            with open(self.config_path) as f:
                return {k: v for k, v in json.load(f).items() if k in DEFAULTS}
        except (OSError, ValueError, AttributeError):
            return {}

    def save_config(self):
        private_dir(DATA_DIR)
        fd = os.open(self.config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.cfg, f, indent=1)
        os.chmod(self.config_path, 0o600)

    def set_option(self, key, value):
        if self.cfg.get(key) == value:
            return
        self.cfg[key] = value
        self.save_config()
        if key == "max_items":
            self.store.prune(value)
        if key == "preview" and self.window:
            self.window.preview_toggled()
        self.refresh()

    # ---------- capturing ----------
    def on_owner_change(self, clipboard, event):
        if not self.tracking:
            return
        if Gdk.selection_owner_get(Gdk.SELECTION_CLIPBOARD) == self.owner.get_window():
            return  # our own restore, nothing new
        self.capture_gen += 1
        if self.debounce_id:
            GLib.source_remove(self.debounce_id)
        self.debounce_id = GLib.timeout_add(150, self.start_capture, self.capture_gen)

    def start_capture(self, gen):
        self.debounce_id = 0
        self.clipboard.request_contents(
            Gdk.Atom.intern("TARGETS", False), self.on_targets, gen)
        return False

    def on_targets(self, clipboard, sel, gen):
        if gen != self.capture_gen or not self.tracking:
            return
        ok, atoms = sel.get_targets()
        if not ok or not atoms:
            return
        names = [a.name() for a in atoms]
        if SECRET_HINTS & set(names):
            return
        todo = select_targets(names)
        if todo:
            self.fetch_next(gen, todo, [], 0)

    def fetch_next(self, gen, todo, got, total):
        if gen != self.capture_gen:
            return
        if not todo:
            if got:
                info = describe({t: d for t, _, _, d in got})
                if self.cfg["save_images"] or info[0] != "image":
                    self.store.add(got, self.cfg["max_items"], info)
                    self.refresh()
            return
        target = todo[0]

        def done(clipboard, sel, _):
            new_total = total
            if gen != self.capture_gen:
                return
            if sel.get_length() >= 0:
                data = sel.get_data() or b""
                if len(data) <= MAX_TARGET_BYTES and total + len(data) <= MAX_ITEM_BYTES:
                    got.append((target, sel.get_data_type().name(), sel.get_format(), bytes(data)))
                    new_total += len(data)
            self.fetch_next(gen, todo[1:], got, new_total)

        self.clipboard.request_contents(Gdk.Atom.intern(target, False), done, None)

    # ---------- restoring ----------
    def restore(self, item_id):
        rows = self.store.formats(item_id)
        if not rows:
            return False
        serving = {t: (dt, f, bytes(d)) for t, dt, f, d in rows}
        # Make sure every common plain-text alias is offered.
        plain = next((serving[t] for t in PLAIN_TARGETS if t in serving), None)
        if plain and plain[1] == 8:
            for t in ("UTF8_STRING", "text/plain;charset=utf-8", "text/plain"):
                serving.setdefault(t, ("UTF8_STRING" if t == "UTF8_STRING" else t, 8, plain[2]))
        self.serving = serving
        sel = Gdk.SELECTION_CLIPBOARD
        Gtk.selection_clear_targets(self.owner, sel)
        for i, t in enumerate(serving):
            Gtk.selection_add_target(self.owner, sel, Gdk.Atom.intern(t, False), i)
        ts = GdkX11.x11_get_server_time(self.owner.get_window())
        if not Gtk.selection_owner_set(self.owner, sel, ts):
            return False
        self.store.touch(item_id)
        self.refresh()
        return True

    def on_selection_get(self, widget, sel, info, ts):
        entry = self.serving.get(sel.get_target().name())
        if entry:
            dtype, fmt, data = entry
            sel.set(Gdk.Atom.intern(dtype, False), fmt, data)

    def on_selection_clear(self, widget, event):
        self.serving = {}
        return False

    # ---------- actions ----------
    def set_tracking(self, on):
        self.set_option("tracking", on)

    def toggle_pin(self, item_id):
        row = self.store.item(item_id)
        if row:
            self.store.set_pinned(item_id, not row[7])
            self.store.prune(self.cfg["max_items"])
            self.refresh()

    def delete_item(self, item_id):
        self.store.delete(item_id)
        self.refresh()

    def clear_all(self, include_pinned=False):
        self.store.clear(include_pinned)
        self.refresh()

    def confirm_clear(self, *_):
        parent = self.window if self.window and self.window.get_visible() else None
        pinned = self.store.pinned_count()
        unpinned = len(self.store.items()) - pinned
        dlg = Gtk.MessageDialog(
            transient_for=parent, modal=True,
            message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.NONE,
            text="Clear all clipboard history?")
        dlg.format_secondary_text(
            f"{unpinned} kept item{'s' if unpinned != 1 else ''} will be deleted. "
            + (f"Your {pinned} pinned item{'s are' if pinned != 1 else ' is'} kept. " if pinned else "")
            + "What is currently on your clipboard stays there.")
        also = None
        if pinned:
            also = Gtk.CheckButton(label=f"Also delete the {pinned} pinned item{'s' if pinned != 1 else ''}")
            also.set_margin_start(12)
            dlg.get_message_area().pack_start(also, False, False, 6)
            also.show()
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        btn = dlg.add_button("Clear All", Gtk.ResponseType.OK)
        btn.get_style_context().add_class("destructive-action")
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        dlg.set_keep_above(True)
        if dlg.run() == Gtk.ResponseType.OK:
            self.clear_all(include_pinned=bool(also and also.get_active()))
        dlg.destroy()

    # ---------- UI ----------
    def refresh(self):
        items = self.store.items()
        self.indicator.set_icon_full(
            "clipkeep-on" if self.tracking else "clipkeep-off",
            "ClipKeep: tracking on" if self.tracking else "ClipKeep: tracking paused")
        self.build_menu(items)
        # A hidden window is rebuilt when it's next shown, not on every copy.
        if self.window and self.window.get_visible():
            self.window.populate(items)

    def build_menu(self, items):
        for child in self.menu.get_children():
            self.menu.remove(child)

        def add(label, cb=None, sensitive=True):
            mi = Gtk.MenuItem(label=menu_text(label), use_underline=True)
            mi.set_sensitive(sensitive)
            if cb:
                mi.connect("activate", cb)
            self.menu.append(mi)
            return mi

        def add_items(rows):
            for row in rows:
                label = "[Image]" if row[2] == "image" else one_line(row[4], 50) or "(empty)"
                if row[2] == "rich":
                    label = "✦ " + label
                add("    " + label, lambda _mi, i=row[0]: self.restore(i))

        key = hotkey_label(get_hotkey())
        open_item = add("Open ClipKeep" + (f"   ({key})" if key else ""),
                        lambda *_: self.show_window())
        self.indicator.set_secondary_activate_target(open_item)
        self.menu.append(Gtk.SeparatorMenuItem())
        pinned = [r for r in items if r[7]]
        recent = [r for r in items if not r[7]][: self.cfg["menu_recent"]]
        if pinned:
            add("★ Pinned — click to copy:", sensitive=False)
            add_items(pinned)
        if recent:
            add("Recent — click to copy:", sensitive=False)
            add_items(recent)
        if not items:
            add("Nothing kept yet", sensitive=False)
        self.menu.append(Gtk.SeparatorMenuItem())
        if self.tracking:
            add("●  Status: Tracking is ON", sensitive=False)
            add("      Pause tracking  → switch to OFF", lambda *_: self.set_tracking(False))
        else:
            add("○  Status: Tracking is PAUSED (OFF)", sensitive=False)
            add("      Resume tracking  → switch to ON", lambda *_: self.set_tracking(True))
        add("Clear all history…", self.confirm_clear, sensitive=bool(items))
        add("Settings…", lambda *_: self.show_settings())
        self.menu.append(Gtk.SeparatorMenuItem())
        add("Quit ClipKeep", lambda *_: self.quit())
        self.menu.show_all()

    def show_window(self):
        if not self.window:
            self.window = HistoryWindow(self)
        self.window.present_now()

    def show_settings(self):
        if not self.settings_window:
            self.settings_window = SettingsWindow(self)
        self.settings_window.present_now()


def present_x11(win):
    """Raise a window even when the request came from a keybinding/CLI."""
    win.show()
    win.set_keep_above(True)
    gdkwin = win.get_window()
    win.present_with_time(GdkX11.x11_get_server_time(gdkwin) if gdkwin else 0)
    GLib.timeout_add(300, lambda: win.set_keep_above(False) or False)


class HistoryWindow(Gtk.ApplicationWindow):
    LIST_WIDTH = 520
    PREVIEW_WIDTH = 480

    def __init__(self, app):
        super().__init__(application=app, title="ClipKeep")
        self.app = app
        self.preview_id = None
        self.hover_timer = 0
        self.toast_timer = 0
        self.thumbs = {}  # item id -> decoded thumbnail pixbuf
        self.get_style_context().add_class("ck-window")
        cfg = app.cfg
        width = cfg["win_width"] or (self.LIST_WIDTH + (self.PREVIEW_WIDTH if cfg["preview"] else 0))
        self.set_default_size(width, cfg["win_height"] or 660)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.connect("delete-event", lambda *_: self.hide_window() or True)
        self.connect("key-press-event", self.on_key)

        header = Gtk.HeaderBar(show_close_button=True, title="ClipKeep")
        self.set_titlebar(header)
        self.header = header
        clear_btn = Gtk.Button(label="Clear All…")
        clear_btn.get_style_context().add_class("destructive-action")
        clear_btn.set_tooltip_text("Delete every unpinned item (asks first)")
        clear_btn.connect("clicked", app.confirm_clear)
        header.pack_end(clear_btn)
        self.clear_btn = clear_btn
        gear = Gtk.Button.new_from_icon_name("emblem-system-symbolic", Gtk.IconSize.BUTTON)
        gear.set_tooltip_text("Settings")
        gear.connect("clicked", lambda *_: app.show_settings())
        header.pack_end(gear)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(box)

        # Tracking banner: says what mode we are in and what the button does.
        self.banner = Gtk.Box(spacing=10)
        self.banner.get_style_context().add_class("ck-banner")
        self.state_label = Gtk.Label(xalign=0)
        self.state_label.get_style_context().add_class("ck-state")
        self.toggle_btn = Gtk.Button()
        self.toggle_btn.connect("clicked", lambda *_: app.set_tracking(not app.tracking))
        self.banner.pack_start(self.state_label, True, True, 0)
        self.banner.pack_end(self.toggle_btn, False, False, 0)
        box.pack_start(self.banner, False, False, 0)

        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        box.pack_start(self.paned, True, True, 0)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.search = Gtk.SearchEntry(placeholder_text="Search — just start typing")
        for side in ("start", "end", "top", "bottom"):
            getattr(self.search, "set_margin_" + side)(8)
        self.search.connect("search-changed", self.on_search_changed)
        self.search.connect("activate", self.on_search_activate)
        left.pack_start(self.search, False, False, 0)

        self.scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.BROWSE)
        self.listbox.set_activate_on_single_click(True)
        self.listbox.set_filter_func(self.filter_row)
        self.listbox.set_header_func(self.header_func)
        self.listbox.connect("row-activated", self.on_row_activated)
        self.listbox.connect("row-selected", lambda *_: self.schedule_preview(None, 0))
        self.placeholder = Gtk.Label(justify=Gtk.Justification.CENTER)
        self.placeholder.get_style_context().add_class("ck-empty")
        self.placeholder.show()
        self.listbox.set_placeholder(self.placeholder)
        self.scroller.add(self.listbox)
        left.pack_start(self.scroller, True, True, 0)
        self.paned.pack1(left, True, False)

        self.preview_box = self.build_preview()
        self.paned.pack2(self.preview_box, True, False)
        self.paned.set_position(cfg["pane"] or self.LIST_WIDTH)

        self.hint = Gtk.Label(xalign=0)
        self.hint.set_margin_start(10)
        self.hint.set_margin_top(4)
        self.hint.set_margin_bottom(6)
        self.hint.set_line_wrap(True)
        box.pack_start(self.hint, False, False, 0)
        box.show_all()
        header.show_all()
        self.preview_box.set_visible(cfg["preview"])
        self.show_hint()

    # ----- preview panel -----
    def build_preview(self):
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        frame.get_style_context().add_class("view")
        self.preview_meta = Gtk.Label(xalign=0)
        self.preview_meta.set_line_wrap(True)
        self.preview_meta.get_style_context().add_class("ck-preview-meta")
        frame.pack_start(self.preview_meta, False, False, 0)
        if WebKit2:
            self.webview = WebKit2.WebView()
            ws = self.webview.get_settings()
            ws.set_enable_javascript(False)
            ws.set_auto_load_images(True)
            ws.set_enable_developer_extras(False)
            # never navigate anywhere (clicked links in the preview do nothing)
            self.webview.connect("decide-policy", self.on_decide_policy)
            self.webview.connect("context-menu", lambda *_: True)
            frame.pack_start(self.webview, True, True, 0)
        else:
            self.webview = None
            self.preview_fallback = Gtk.Label(label="Preview needs gir1.2-webkit2-4.1", vexpand=True)
            frame.pack_start(self.preview_fallback, True, True, 0)
        return frame

    def on_decide_policy(self, view, decision, dtype):
        if dtype == WebKit2.PolicyDecisionType.RESPONSE:
            return False
        req = decision.get_navigation_action().get_request().get_uri() \
            if hasattr(decision, "get_navigation_action") else ""
        if req.startswith("about:") or req.startswith("data:"):
            return False
        decision.ignore()
        return True

    def preview_toggled(self):
        """Grow or shrink the window by the preview's width, keeping the
        list the size it was."""
        on = self.app.cfg["preview"]
        w, h = self.get_size()
        list_w = self.paned.get_position() if self.preview_box.get_visible() else w
        self.preview_box.set_visible(on)
        self.resize(list_w + self.PREVIEW_WIDTH if on else list_w, h)
        if on:
            self.paned.set_position(list_w)
            self.show_preview(self.preview_id, force=True)
        self.show_hint()

    def show_hint(self):
        self.hint.get_style_context().remove_class("ck-toast")
        self.hint.get_style_context().add_class("ck-hint")
        self.hint.set_text("Click an item to copy it (formatting kept), then paste with Ctrl+V.   "
                           "☆ pins · ✕ removes" + (" · hover to preview" if self.app.cfg["preview"] else ""))
        self.toast_timer = 0
        return False

    def toast(self, text):
        if self.toast_timer:
            GLib.source_remove(self.toast_timer)
        self.hint.get_style_context().remove_class("ck-hint")
        self.hint.get_style_context().add_class("ck-toast")
        self.hint.set_text(text)
        self.toast_timer = GLib.timeout_add(2500, self.show_hint)

    def schedule_preview(self, item_id, delay):
        """item_id None = show the selected row."""
        if self.hover_timer:
            GLib.source_remove(self.hover_timer)
            self.hover_timer = 0

        def run():
            self.hover_timer = 0
            target = item_id
            if target is None:
                row = self.listbox.get_selected_row()
                target = row.item_id if row else None
            self.show_preview(target)
            return False

        if delay:
            self.hover_timer = GLib.timeout_add(delay, run)
        else:
            run()

    def show_preview(self, item_id, force=False):
        if not self.app.cfg["preview"] or (item_id == self.preview_id and not force):
            return
        self.preview_id = item_id
        row = self.app.store.item(item_id) if item_id is not None else None
        if not row:
            self.preview_meta.set_markup("<i>Select or hover an item to preview it.</i>")
            self.preview_meta.set_tooltip_text(None)
            if self.webview:
                self.webview.load_html(PREVIEW_HEAD + "</body></html>", "about:blank")
            return
        _, ts, kind, badge, text, size, _, pinned = row
        targets, formats = self.app.store.preview_formats(item_id)
        pin = "★ Pinned  ·  " if pinned else ""
        saved = ", ".join(friendly_formats(targets))
        self.preview_meta.set_markup(
            f"<b>{pin}{GLib.markup_escape_text(badge)}</b>  ·  {time_ago(ts)}  ·  {human_size(size)}\n"
            f"<small>Pastes as: {GLib.markup_escape_text(saved)}</small>")
        self.preview_meta.set_tooltip_text("Exact clipboard formats kept:\n" + "\n".join(targets))
        if self.webview:
            self.webview.load_html(preview_html(kind, formats), "about:blank")

    def present_now(self):
        self.search.set_text("")
        self.populate(self.app.store.items())
        present_x11(self)
        self.search.grab_focus()
        first = self.listbox.get_row_at_index(0)
        if first:
            self.listbox.select_row(first)
            self.scroller.get_vadjustment().set_value(0)
        self.show_preview(first.item_id if first else None, force=True)

    def hide_window(self):
        """Hide, remembering size and divider for next time."""
        cfg = self.app.cfg
        w, h = self.get_size()
        cfg["win_width"], cfg["win_height"] = w, h
        if self.preview_box.get_visible():
            cfg["pane"] = self.paned.get_position()
        self.app.save_config()
        self.hide()

    def update_banner(self):
        ctx = self.banner.get_style_context()
        ctx.remove_class("on")
        ctx.remove_class("off")
        if self.app.tracking:
            ctx.add_class("on")
            self.state_label.set_text("●  Tracking is ON — new copies are kept")
            self.toggle_btn.set_label("Pause  (switch to OFF)")
        else:
            ctx.add_class("off")
            self.state_label.set_text("●  Tracking is PAUSED — new copies are NOT kept")
            self.toggle_btn.set_label("Resume  (switch to ON)")

    def populate(self, items):
        self.update_banner()
        pinned = sum(1 for r in items if r[7])
        self.header.set_subtitle(f"{len(items) - pinned} of {self.app.cfg['max_items']} kept"
                                 + (f"  ·  {pinned} pinned" if pinned else ""))
        self.clear_btn.set_sensitive(bool(items))
        self.has_pins = bool(pinned)
        self.update_placeholder()
        selected = self.listbox.get_selected_row()
        sel_id = selected.item_id if selected else None
        adj = self.scroller.get_vadjustment()
        scroll = adj.get_value()
        live = {r[0] for r in items}
        self.thumbs = {k: v for k, v in self.thumbs.items() if k in live}
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        for row in items:
            self.listbox.add(self.make_row(row))
        for r in self.listbox.get_children():
            if r.item_id == sel_id:
                self.listbox.select_row(r)
        self.listbox.show_all()
        GLib.idle_add(lambda: adj.set_value(min(scroll, adj.get_upper())) and False)
        if self.get_visible():
            current = self.listbox.get_selected_row()
            self.show_preview(current.item_id if current else None, force=True)

    def update_placeholder(self):
        if self.search.get_text().strip():
            self.placeholder.set_text("No kept item matches your search.")
        else:
            self.placeholder.set_text("Nothing kept yet.\nCopy something and it will show up here.")

    def header_func(self, row, before):
        if not self.has_pins or (before is not None and before.pinned == row.pinned):
            row.set_header(None)
            return
        label = Gtk.Label(label="★  PINNED" if row.pinned else "HISTORY", xalign=0)
        label.get_style_context().add_class("ck-section")
        row.set_header(label)

    def thumb_for(self, item_id, data):
        if item_id not in self.thumbs:
            try:
                self.thumbs[item_id] = pixbuf_from_png(data)
            except GLib.Error:
                self.thumbs[item_id] = None
        return self.thumbs[item_id]

    def make_row(self, row):
        item_id, ts, kind, badge, text, size, thumb, pinned = row
        lbrow = Gtk.ListBoxRow()
        lbrow.item_id = item_id
        lbrow.pinned = bool(pinned)
        extra = {"image": " image picture", "files": " files"}.get(kind, "")
        lbrow.search_text = (text + extra).lower()
        lbrow.get_style_context().add_class("ck-row")
        events = Gtk.EventBox(above_child=False, visible_window=False)
        events.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        events.connect("enter-notify-event", lambda *_: self.schedule_preview(item_id, 120))
        events.connect("leave-notify-event", self.on_row_leave)
        lbrow.add(events)
        hbox = Gtk.Box(spacing=10)
        events.add(hbox)

        pixbuf = self.thumb_for(item_id, thumb) if thumb else None
        if pixbuf:
            hbox.pack_start(Gtk.Image.new_from_pixbuf(pixbuf), False, False, 0)
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        hbox.pack_start(vbox, True, True, 0)
        if kind == "image":
            preview = f"Image ({size // 1024 or 1} KB)"
        else:
            lines = [ln.rstrip().expandtabs(4) for ln in text.splitlines() if ln.strip()]
            preview = "\n".join(ln if len(ln) <= 200 else ln[:199] + "…"
                                for ln in lines[:3]) or "(empty text)"
        main = Gtk.Label(label=preview, xalign=0)
        main.set_line_wrap(True)
        main.set_lines(3)
        main.set_ellipsize(3)  # Pango.EllipsizeMode.END
        main.set_max_width_chars(60)
        vbox.pack_start(main, False, False, 0)
        meta = Gtk.Label(xalign=0)
        cls = {"rich": "ck-badge-rich", "image": "ck-badge-image"}.get(kind)
        if cls:
            meta.get_style_context().add_class(cls)
        meta.set_markup(f"{GLib.markup_escape_text(badge)}  ·  {time_ago(ts)}")
        meta.get_style_context().add_class("ck-meta")
        vbox.pack_start(meta, False, False, 0)

        rm = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.BUTTON)
        rm.set_relief(Gtk.ReliefStyle.NONE)
        rm.set_valign(Gtk.Align.CENTER)
        rm.set_tooltip_text("Remove this item from history (Delete)")
        rm.connect("clicked", lambda *_: self.app.delete_item(item_id))
        hbox.pack_end(rm, False, False, 0)

        pin = Gtk.Button.new_from_icon_name(
            "starred-symbolic" if pinned else "non-starred-symbolic", Gtk.IconSize.BUTTON)
        pin.set_relief(Gtk.ReliefStyle.NONE)
        pin.set_valign(Gtk.Align.CENTER)
        pin.set_tooltip_text("Unpin (back to normal history)" if pinned
                             else "Pin: keep at the top, never auto-removed (Ctrl+P)")
        if pinned:
            pin.get_style_context().add_class("ck-pinned-star")
        pin.connect("clicked", lambda *_: self.app.toggle_pin(item_id))
        hbox.pack_end(pin, False, False, 0)
        return lbrow

    def on_row_leave(self, widget, event):
        if event.detail != Gdk.NotifyType.INFERIOR:
            self.schedule_preview(None, 200)
        return False

    def filter_row(self, row):
        q = self.search.get_text().strip().lower()
        return not q or q in row.search_text

    def visible_rows(self):
        return [r for r in self.listbox.get_children() if r.get_child_visible()]

    def on_search_changed(self, *_):
        self.listbox.invalidate_filter()
        self.listbox.invalidate_headers()
        self.update_placeholder()
        selected = self.listbox.get_selected_row()
        if not selected or not selected.get_child_visible():
            rows = self.visible_rows()
            if rows:
                self.listbox.select_row(rows[0])
            else:
                self.listbox.unselect_all()
                self.show_preview(None)

    def on_search_activate(self, *_):
        row = self.listbox.get_selected_row()
        if not row or not row.get_child_visible():
            rows = self.visible_rows()
            row = rows[0] if rows else None
        if row:
            self.on_row_activated(self.listbox, row)

    def on_row_activated(self, listbox, row):
        if not self.app.restore(row.item_id):
            self.toast("Couldn't put that item on the clipboard — try again.")
        elif self.app.cfg["close_after_copy"]:
            self.hide_window()
        else:
            self.toast("✓ Copied — paste it with Ctrl+V")

    def on_key(self, widget, event):
        key = event.keyval
        state = event.state
        ctrl = state & Gdk.ModifierType.CONTROL_MASK
        if key == Gdk.KEY_Escape:
            if self.search.get_text():
                self.search.set_text("")  # first Esc clears the search
                self.search.grab_focus()
            else:
                self.hide_window()
            return True
        if ctrl and key in (Gdk.KEY_p, Gdk.KEY_P):
            row = self.listbox.get_selected_row()
            if row:
                self.app.toggle_pin(row.item_id)
            return True
        if key == Gdk.KEY_Delete and not self.search.has_focus():
            row = self.listbox.get_selected_row()
            if row:
                idx = row.get_index()
                self.app.delete_item(row.item_id)
                nxt = self.listbox.get_row_at_index(min(idx, len(self.listbox.get_children()) - 1))
                if nxt:
                    self.listbox.select_row(nxt)
                    nxt.grab_focus()
            return True
        if key == Gdk.KEY_Down and self.search.has_focus():
            row = self.listbox.get_selected_row() or (self.visible_rows() or [None])[0]
            if row:
                row.grab_focus()
            return True
        # Type anywhere to search (Space still activates a focused row).
        if (not self.search.has_focus() and not (state & (Gdk.ModifierType.CONTROL_MASK
                                                         | Gdk.ModifierType.MOD1_MASK))
                and Gdk.keyval_to_unicode(key) >= 32 and key != Gdk.KEY_space):
            self.search.grab_focus_without_selecting()
            return self.search.handle_event(event)
        return False


class SettingsWindow(Gtk.Window):
    """Changes apply immediately."""

    def __init__(self, app):
        super().__init__(title="ClipKeep Settings")
        self.app = app
        self.get_style_context().add_class("ck-window")
        # Resizable on purpose: GTK sizes a non-resizable window by its
        # minimum layout, which leaves a blank band under the wrapped text.
        self.set_position(Gtk.WindowPosition.CENTER)
        self.connect("delete-event", lambda *_: self.hide() or True)
        self.connect("key-press-event",
                     lambda _w, e: (self.hide() or True) if e.keyval == Gdk.KEY_Escape else False)
        header = Gtk.HeaderBar(show_close_button=True, title="ClipKeep Settings")
        self.set_titlebar(header)

        self.grid = Gtk.Grid(column_spacing=24, row_spacing=12)
        for side in ("start", "end", "top", "bottom"):
            getattr(self.grid, "set_margin_" + side)(20)
        self.add(self.grid)
        self.row = 0

        self.add_group("Shortcut")
        self.hotkey_btn = Gtk.Button()
        self.hotkey_btn.connect("clicked", self.change_hotkey)
        self.add_row("Open-window shortcut",
                     "Click, then press the new key combination.", self.hotkey_btn)

        self.add_group("History")
        self.add_row("History size",
                     "Unpinned items kept. Pinned items don't count. "
                     "Lowering it deletes the oldest extras.",
                     self.spin("max_items", 10, 1000, 10))
        self.add_row("Save copied images", "Screenshots and pictures. Formatted text is always kept.",
                     self.switch("save_images"))
        self.add_row("Keep history after restart",
                     "Off: unpinned history is cleared each time ClipKeep starts.",
                     self.switch("persist"))

        self.add_group("Window and menu")
        self.add_row("Preview panel", "Show the selected or hovered item, fully formatted.",
                     self.switch("preview"))
        self.add_row("Close window after copying",
                     "Off: the window stays open so you can copy several items.",
                     self.switch("close_after_copy"))
        self.add_row("Items in tray menu", "Recent items listed when you click the tray icon.",
                     self.spin("menu_recent", 0, 30, 1))

        self.add_group("Startup")
        auto = Gtk.Switch(active=os.path.exists(AUTOSTART_FILE), valign=Gtk.Align.CENTER)
        auto.connect("notify::active", lambda s, _: set_autostart(s.get_active()))
        self.add_row("Start at login", "Launch ClipKeep automatically when you log in.", auto)

        self.footer = Gtk.Label(xalign=0)
        self.footer.get_style_context().add_class("ck-settings-foot")
        self.footer.set_margin_top(8)
        self.grid.attach(self.footer, 0, self.row, 2, 1)
        self.grid.show_all()
        header.show_all()

    def add_group(self, title):
        label = Gtk.Label(label=title, xalign=0)
        label.get_style_context().add_class("ck-settings-group")
        if self.row:
            label.set_margin_top(10)
        self.grid.attach(label, 0, self.row, 2, 1)
        self.row += 1

    def add_row(self, title, desc, widget):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        t = Gtk.Label(label=title, xalign=0)
        d = Gtk.Label(label=desc, xalign=0)
        d.set_line_wrap(True)
        d.set_width_chars(54)  # wrap at a fixed width so no blank space is reserved
        d.set_max_width_chars(54)
        d.get_style_context().add_class("ck-settings-desc")
        box.pack_start(t, False, False, 0)
        box.pack_start(d, False, False, 0)
        widget.set_halign(Gtk.Align.END)
        widget.set_valign(Gtk.Align.CENTER)
        self.grid.attach(box, 0, self.row, 1, 1)
        self.grid.attach(widget, 1, self.row, 1, 1)
        self.row += 1

    def spin(self, key, lo, hi, step):
        """Applied once the value settles, so stepping 100 → 10 doesn't delete
        items at every click on the way down."""
        s = Gtk.SpinButton.new_with_range(lo, hi, step)
        s.set_value(self.app.cfg[key])
        pending = [0]

        def apply():
            pending[0] = 0
            self.app.set_option(key, int(s.get_value()))
            self.update_footer()
            return False

        def changed(_w):
            if pending[0]:
                GLib.source_remove(pending[0])
            pending[0] = GLib.timeout_add(900, apply)

        s.connect("value-changed", changed)
        return s

    def switch(self, key):
        s = Gtk.Switch(active=self.app.cfg[key])
        s.connect("notify::active", lambda w, _: self.app.set_option(key, w.get_active()))
        return s

    def update_hotkey_label(self):
        label = hotkey_label(get_hotkey())
        self.hotkey_btn.set_label(label or "Not set — click to set")

    def update_footer(self):
        self.footer.set_text(f"ClipKeep {VERSION}  ·  history uses "
                             f"{human_size(self.app.store.disk_usage())} on disk")

    def change_hotkey(self, *_):
        dlg = ShortcutDialog(self)
        if dlg.run() == Gtk.ResponseType.OK:
            set_hotkey(dlg.result)
            self.app.refresh()
        dlg.destroy()
        self.update_hotkey_label()

    def present_now(self):
        self.update_hotkey_label()
        self.update_footer()
        present_x11(self)


class ShortcutDialog(Gtk.Dialog):
    MODIFIER_KEYS = ("Shift", "Control", "Alt", "Super", "Meta", "Hyper", "ISO_", "Caps", "Num")

    def __init__(self, parent):
        super().__init__(title="Set shortcut", transient_for=parent, modal=True)
        self.result = None
        self.set_default_size(380, -1)
        area = self.get_content_area()
        area.set_spacing(10)
        for side in ("start", "end", "top", "bottom"):
            getattr(area, "set_margin_" + side)(18)
        msg = Gtk.Label(xalign=0)
        msg.set_markup("<b>Press the new shortcut for opening ClipKeep.</b>\n\n"
                       "Use Ctrl, Alt or Super with a key.\n"
                       "<small>Esc cancels · Backspace removes the shortcut</small>")
        area.pack_start(msg, False, False, 0)
        self.status = Gtk.Label(xalign=0)
        self.status.set_line_wrap(True)
        self.status.get_style_context().add_class("ck-warn")
        area.pack_start(self.status, False, False, 0)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.connect("key-press-event", self.on_key)
        self.show_all()

    def on_key(self, widget, event):
        keymap = Gdk.Keymap.get_for_display(self.get_display())
        state = keymap.add_virtual_modifiers(event.state)
        # XWayland reports Super as Super+Hyper; keep only the modifiers GNOME
        # shortcuts use so the stored binding and the conflict check match.
        mods = state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK
                        | Gdk.ModifierType.MOD1_MASK | Gdk.ModifierType.SUPER_MASK)
        kv = Gdk.keyval_to_lower(event.keyval)
        name = Gdk.keyval_name(kv) or ""
        if not mods and kv == Gdk.KEY_Escape:
            self.response(Gtk.ResponseType.CANCEL)
            return True
        if not mods and kv == Gdk.KEY_BackSpace:
            self.result = ""
            self.response(Gtk.ResponseType.OK)
            return True
        if name.startswith(self.MODIFIER_KEYS):
            return True
        needed = (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK
                  | Gdk.ModifierType.SUPER_MASK)
        if not (mods & needed) and not re.fullmatch(r"F\d+", name):
            self.status.set_text("Add Ctrl, Alt or Super to that key.")
            return True
        accel = Gtk.accelerator_name(kv, mods)
        clash = hotkey_conflict(accel)
        if clash:
            self.status.set_text(f"{hotkey_label(accel)} is already used by GNOME ({clash}). "
                                 "Try another combination.")
            return True
        self.result = accel
        self.response(Gtk.ResponseType.OK)
        return True


def standalone_command(arg):
    """Commands that must not start (or need) the running app. Returns an
    exit code, or None if arg is an app command."""
    if arg == "--version":
        print(f"ClipKeep {VERSION}")
    elif arg == "--hotkey-default":
        current = get_hotkey()
        clash = None if current else hotkey_conflict(DEFAULT_HOTKEY)
        if current:
            print(f"Shortcut already set: {hotkey_label(current)}")
        elif clash:
            print(f"{hotkey_label(DEFAULT_HOTKEY)} is taken ({clash}); "
                  "set a shortcut in ClipKeep's Settings.")
        else:
            set_hotkey(DEFAULT_HOTKEY)
            print(f"Shortcut set: {hotkey_label(DEFAULT_HOTKEY)}")
    elif arg == "--hotkey-remove":
        remove_hotkey()
        print("Shortcut removed.")
    else:
        return None
    return 0


if __name__ == "__main__":
    code = standalone_command(sys.argv[1]) if len(sys.argv) == 2 else None
    sys.exit(code if code is not None else ClipKeep().run(sys.argv))
