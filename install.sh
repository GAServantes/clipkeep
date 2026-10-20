#!/usr/bin/env bash
# Copyright (C) 2026 G. Austin Servantes
# SPDX-License-Identifier: GPL-3.0-or-later
# Install ClipKeep for the current user: app-menu entry, icon, `clipkeep`
# command, start at login and the Ctrl+Alt+V shortcut. Run from the repo; the
# app keeps running from this folder.
#   ./install.sh            install (and start it)
#   ./install.sh --remove   remove the launcher, icon, command, autostart entry and shortcut
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
BIN="$HOME/.local/bin"
AUTOSTART="$HOME/.config/autostart"

if [[ "${1:-}" == "--remove" ]]; then
  "$HERE/clipkeep.py" --quit 2>/dev/null || true
  "$HERE/clipkeep.py" --hotkey-remove || true
  rm -f "$APPS/clipkeep.desktop" "$ICONS/clipkeep.svg" "$BIN/clipkeep" "$AUTOSTART/clipkeep.desktop"
  echo "ClipKeep removed. Your history stays in ~/.local/share/clipkeep (delete that folder to erase it)."
  exit 0
fi

python3 - <<'PY' || {
import gi
gi.require_version("Gtk", "3.0"); gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import Gtk, AyatanaAppIndicator3  # noqa: F401
PY
  echo "Missing GTK/AppIndicator bindings. Install them with:"
  echo "  sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 gir1.2-webkit2-4.1"
  exit 1
}
python3 -c 'import gi; gi.require_version("WebKit2", "4.1")' 2>/dev/null ||
  echo "Note: gir1.2-webkit2-4.1 isn't installed, so the preview panel will be empty."
if ! gnome-extensions list --enabled 2>/dev/null | grep -q appindicator; then
  echo "Note: the AppIndicator GNOME extension isn't enabled; the tray icon needs it"
  echo "      (Ubuntu ships it as ubuntu-appindicators@ubuntu.com)."
fi

mkdir -p "$APPS" "$ICONS" "$BIN" "$AUTOSTART"
cp "$HERE/icons/clipkeep-on.svg" "$ICONS/clipkeep.svg"
ln -sf "$HERE/clipkeep.py" "$BIN/clipkeep"
cat > "$APPS/clipkeep.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=ClipKeep
Comment=Clipboard history that keeps formatting
Exec="$HERE/clipkeep.py" --show
Icon=clipkeep
Terminal=false
Categories=Utility;
Keywords=clipboard;history;copy;paste;
Actions=Settings;

[Desktop Action Settings]
Name=Settings
Exec="$HERE/clipkeep.py" --settings
DESK
cat > "$AUTOSTART/clipkeep.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=ClipKeep
Comment=Clipboard history
Exec="$HERE/clipkeep.py"
Icon=clipkeep
Terminal=false
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=3
DESK
gtk-update-icon-cache -q "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
update-desktop-database -q "$APPS" 2>/dev/null || true
"$HERE/clipkeep.py" --hotkey-default
nohup "$HERE/clipkeep.py" >/dev/null 2>&1 &
echo "ClipKeep installed and started. Copy something, then open it from the tray icon."
