# ClipKeep — development notes

Read this before changing ClipKeep. It covers how the app works, where its
data lives, how to test it without touching your real history, and the traps
hit so far.

## Files and paths

| What | Where |
|---|---|
| App | `clipkeep.py` (single file; `install.sh` symlinks it as `~/.local/bin/clipkeep`) |
| Tray icons | `icons/clipkeep-on.svg`, `icons/clipkeep-off.svg` (the clip is a keep's battlements) |
| History DB | `~/.local/share/clipkeep/history.db` (SQLite, mode 600, folder 700) |
| Settings | `~/.local/share/clipkeep/config.json` (mode 600; missing = defaults) |
| App menu entry | `~/.local/share/applications/clipkeep.desktop` (written by `install.sh`) |
| Autostart | `~/.config/autostart/clipkeep.desktop` (`install.sh`, or the "Start at login" setting) |
| Hotkey | GNOME custom keybinding at `/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/clipkeep/`, runs `clipkeep.py --show` |

- App ID / D-Bus name: `io.github.gaservantes.ClipKeep`. Versions before
  2.1.0 used `io.github.local.ClipKeep`.
- `VERSION` near the top of `clipkeep.py`. Bump it, plus the README
  changelog, on every change.

## How it works

### Why XWayland (`GDK_BACKEND=x11`)
GNOME's compositor (Mutter) has no `wlr-data-control` protocol, and plain
Wayland clients only see the clipboard while focused. Mutter *does* mirror the
Wayland clipboard into XWayland's X11 CLIPBOARD selection, both ways. So:
- **Watching:** `Gtk.Clipboard` `owner-change` (XFixes) fires for every copy,
  with no focus needed.
- **Restoring:** a hidden `Gtk.Invisible` takes CLIPBOARD ownership
  (`Gtk.selection_owner_set`) and answers `selection-get` for each stored
  target. Mutter bridges that to Wayland apps.

`os.environ["GDK_BACKEND"] = "x11"` must stay above the Gtk import.

### Capture pipeline
`on_owner_change` → 150 ms debounce → request `TARGETS` → `select_targets()`
→ each target is fetched one after another, asynchronously (`fetch_next`) →
`Store.add`.
- `capture_gen` drops a stale capture if another copy lands mid-fetch.
- Skipped: meta targets, PIXMAP/BITMAP, and every image type except
  `image/png` (or the first image type if there's no PNG).
- Items advertising `x-kde-passwordManagerHint` are never recorded.
- Size caps: 32 MB per target, 64 MB per item. Rich and plain text are
  fetched first, so a cap can't drop them.
- Copies that come from ClipKeep itself are ignored (owner-window check).
- `describe()` runs once per capture and is passed into `Store.add` /
  `content_hash`.

### Dedup (`content_hash`)
The hash covers the decoded plain text plus the raw rich (HTML/RTF),
file-list and image bytes. It ignores legacy text aliases (STRING, TEXT,
COMPOUND_TEXT).
- Same content again → the existing row's timestamp is bumped.
- Same words with different formatting → separate items.
- Changing this function leaves old rows with old hashes; re-hash them with a
  one-off script if needed.

### DB schema
- `items`: id, ts, hash (unique), kind (`text`/`rich`/`image`/`files`), badge,
  text (≤ 20k chars, for display and search), size, thumb (PNG ≤ 96 px),
  pinned (0, or the time it was pinned).
- `formats`: item_id (cascade delete), pos, target, dtype, fmt, data.
- `pinned` is added with `ALTER TABLE` if missing (v1 databases).
- Pruning deletes only unpinned rows beyond `max_items`.
- On startup, `VACUUM` runs if more than a quarter of the file is free pages.

### Preview
- WebKit2 4.1 with JavaScript off. A CSP `<meta>` (`default-src 'none'`,
  `data:` images only) blocks every network load. `decide-policy` blocks
  navigation.
- `strip_html_scaffolding()` removes any copied `<meta>`, `<base>`, `<link>`,
  `<script>` and `<head>`, so copied HTML can't override the policy.
- `Store.preview_formats()` loads only the blobs the preview shows (HTML, one
  plain text, files, image), never e.g. a 30 MB LibreOffice embed.

### Window
- `refresh()` rebuilds the list only while the window is visible;
  `present_now()` rebuilds on show.
- Thumbnail pixbufs are cached per item id. Scroll position is kept across
  rebuilds.
- Size and pane divider are saved in `hide_window()` (`win_width`,
  `win_height`, `pane`).

### Deliberately not done
- **No clipboard reclaiming** when the source app exits. Mutter keeps the
  content, and grabbing ownership breaks apps (LibreOffice) that use richer
  internal paste formats.
- **No auto-paste.** Wayland forbids synthetic input into other apps.

## Testing

```bash
python3 -m unittest discover -s tests   # pure logic + Store, instant
python3 tests/roundtrip.py              # isolated live instance, ~20 s
```

`roundtrip.py` starts ClipKeep with `CLIPKEEP_HOME=<tmp>` and
`CLIPKEEP_APP_ID=io.github.gaservantes.ClipKeepTest`, then plays a fake app
that owns the clipboard (formatted text, then an image). It checks capture,
byte-identical restore, pin/clear, pause and file permissions.

**Quit your real ClipKeep first** (`clipkeep --quit`, then restart it with
`setsid ~/clipkeep/clipkeep.py &`). Otherwise it records the test copies.
Anything you copy during a test lands in the throwaway test DB.

### Manual / visual checks
- **Isolated instance:** `CLIPKEEP_HOME=<tmp> CLIPKEEP_APP_ID=…Demo python3 clipkeep.py`.
  Pass the same env vars to CLI calls aimed at it. Seed demo data with the
  `Store` API (no capture needed), and use only made-up data for anything
  published (README screenshots).
- **Screenshot one window:** `Gdk.pixbuf_get_from_window` on
  `GdkX11.X11Window.foreign_new_for_display(display, xid)`. It captures that
  window only.
- **Clicks and keys:** `xdotool`.
  - `xdotool mousemove --window W x y click 1`, with coordinates in the
    window's frame including its CSD shadow (the same frame as the
    screenshot).
  - Keys: `xdotool windowactivate --sync W; xdotool key …`.
    `xdotool key --window W` sends synthetic events, which GTK ignores.
  - `--show` raises the history window over Settings. Re-raise Settings
    (`--settings`) before clicking it.
- **What the tray menu really shows:** read the exported menu and apply the
  extension's label rule:
  ```bash
  ITEM=$(gdbus call --session --dest org.kde.StatusNotifierWatcher --object-path /StatusNotifierWatcher \
    --method org.freedesktop.DBus.Properties.Get org.kde.StatusNotifierWatcher RegisteredStatusNotifierItems \
    | grep -o ":[0-9.]*@[^']*ClipKeep")
  gdbus call --session --dest ${ITEM%%@*} --object-path ${ITEM#*@}/Menu \
    --method com.canonical.dbusmenu.GetLayout -- 0 -1 "['label']"   # note the -- before -1
  ```
- **Real-browser test:** a browser only writes the clipboard while it's the
  *focused* Wayland window, so unfocused automation can't copy. Instead,
  launch a throwaway browser on X11
  (`brave --ozone-platform=x11 --user-data-dir=<tmp>`), `windowactivate`, and
  send ctrl+c / ctrl+v with xdotool.

### GTK 3 / GNOME gotchas hit so far
- **Header bars:** `box.show_all()` doesn't show a `HeaderBar` set via
  `set_titlebar`. Call `header.show_all()` too.
- **Super key:** XWayland reports Super as Super+Hyper. The shortcut dialog
  masks modifiers to Ctrl/Shift/Alt/Super so the stored binding and the
  conflict check match.
- **PyGObject API quirks:**
  - `Gtk.selection_add_target(widget, …)` is a module function, not a widget
    method.
  - `Keymap.add_virtual_modifiers()` returns the new state; it doesn't change
    it in place.
- **Window names:** a window's X11 name is its HeaderBar title (search for
  `'ClipKeep Settings'`).
- **Tray menu underscores:**
  - libdbusmenu doubles every `_` in a non-mnemonic label, and the
    AppIndicator extension then strips only the *first* `_X`.
  - Fix: `use_underline=True` plus `menu_text()`'s single throwaway leading
    `_`.
- **Non-resizable windows:** GTK sizes a non-resizable window by its minimum
  layout, leaving a blank band under wrapped labels. Settings is resizable on
  purpose, with description labels at fixed `width_chars`.
- **Test scripts:** pin `Gdk` to 3.0 before importing `GdkX11`, or GTK 4 may
  load first.
