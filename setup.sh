#!/usr/bin/env bash
# couchpi setup script
# Installs dependencies, writes default config, patches Labwc autostart and rc.xml

set -euo pipefail

LAUNCHER_SRC="$(cd "$(dirname "$0")" && pwd)/launcher.py"
CONFIG_DIR="$HOME/.config/couchpi"
LABWC_DIR="$HOME/.config/labwc"
AUTOSTART="$LABWC_DIR/autostart"
RC_XML="$LABWC_DIR/rc.xml"
TOGGLE_SCRIPT="$CONFIG_DIR/toggle.py"

ok()   { echo "[OK]   $*"; }
info() { echo "[INFO] $*"; }
fail() { echo "[FAIL] $*" >&2; }

# ---------------------------------------------------------------------------
# 1. Install apt dependencies
# ---------------------------------------------------------------------------

info "Installing apt dependencies..."
sudo apt-get update -qq
if sudo apt-get install -y \
    python3-gi \
    python3-gi-cairo \
    gir1.2-gtk-4.0 \
    gir1.2-gtklayershell-0.1 \
    python3-evdev; then
    ok "apt dependencies installed"
else
    fail "apt install failed — check your internet connection and apt sources"
    exit 1
fi

# ---------------------------------------------------------------------------
# 2. Ensure config directory exists
# ---------------------------------------------------------------------------

mkdir -p "$CONFIG_DIR/icons"
ok "Config directory: $CONFIG_DIR"

# ---------------------------------------------------------------------------
# 3. Write default apps.json if missing
# ---------------------------------------------------------------------------

APPS_JSON="$CONFIG_DIR/apps.json"
if [ ! -f "$APPS_JSON" ]; then
    cat > "$APPS_JSON" <<'JSON'
[
  {
    "name": "RetroArch",
    "launch": "retroarch",
    "icon": null
  },
  {
    "name": "Jellyfin",
    "url": "http://localhost:8096",
    "launch": "chromium --kiosk http://localhost:8096",
    "icon": null
  }
]
JSON
    ok "Written default apps.json"
else
    info "apps.json already exists, skipping"
fi

# ---------------------------------------------------------------------------
# 4. Install launcher.py to config dir
# ---------------------------------------------------------------------------

cp "$LAUNCHER_SRC" "$CONFIG_DIR/launcher.py"
chmod +x "$CONFIG_DIR/launcher.py"
ok "Installed launcher.py → $CONFIG_DIR/launcher.py"

# ---------------------------------------------------------------------------
# 5. Write toggle.py (used by the Home key binding)
# ---------------------------------------------------------------------------

cat > "$TOGGLE_SCRIPT" <<'PYEOF'
#!/usr/bin/env python3
"""Connect to couchpi's IPC socket to toggle visibility.
If couchpi is not running, launch it instead."""
import socket, subprocess, sys, os

SOCKET_PATH = os.path.expanduser("~/.config/couchpi/couchpi.sock")
LAUNCHER = os.path.expanduser("~/.config/couchpi/launcher.py")

try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCKET_PATH)
    s.close()
except (FileNotFoundError, ConnectionRefusedError):
    subprocess.Popen(
        ["python3", LAUNCHER],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
PYEOF
chmod +x "$TOGGLE_SCRIPT"
ok "Written toggle.py → $TOGGLE_SCRIPT"

# ---------------------------------------------------------------------------
# 6. Patch Labwc autostart
# ---------------------------------------------------------------------------

mkdir -p "$LABWC_DIR"
touch "$AUTOSTART"

AUTOSTART_LINE="python3 $CONFIG_DIR/launcher.py &"
if grep -qF "$CONFIG_DIR/launcher.py" "$AUTOSTART"; then
    info "autostart already contains launcher, skipping"
else
    echo "$AUTOSTART_LINE" >> "$AUTOSTART"
    ok "Added launcher to $AUTOSTART"
fi

# ---------------------------------------------------------------------------
# 7. Patch Labwc rc.xml with Home key binding
# ---------------------------------------------------------------------------

touch "$RC_XML"

HOME_KEYBIND='    <keybind key="Home">
      <action name="Execute">
        <command>python3 '"$TOGGLE_SCRIPT"'</command>
      </action>
    </keybind>'

if grep -q "couchpi\|toggle.py" "$RC_XML" 2>/dev/null; then
    info "rc.xml already contains couchpi binding, skipping"
elif [ ! -s "$RC_XML" ]; then
    # File is empty — write a minimal rc.xml
    cat > "$RC_XML" <<XML
<?xml version="1.0" encoding="UTF-8"?>
<openbox_config xmlns="http://openbox.org/3.4/rc">
  <keyboard>
$HOME_KEYBIND
  </keyboard>
</openbox_config>
XML
    ok "Written minimal rc.xml with Home binding"
else
    # File exists — insert before </keyboard> if present, else before </openbox_config>
    if grep -q "</keyboard>" "$RC_XML"; then
        sed -i "s|</keyboard>|$HOME_KEYBIND\n  </keyboard>|" "$RC_XML"
        ok "Inserted Home binding into <keyboard> block in rc.xml"
    else
        sed -i "s|</openbox_config>|  <keyboard>\n$HOME_KEYBIND\n  </keyboard>\n</openbox_config>|" "$RC_XML"
        ok "Created <keyboard> block and inserted Home binding in rc.xml"
    fi
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
echo "couchpi setup complete!"
echo "  Config:     $CONFIG_DIR"
echo "  Launcher:   $CONFIG_DIR/launcher.py"
echo "  Apps:       $APPS_JSON"
echo "  Autostart:  $AUTOSTART"
echo "  rc.xml:     $RC_XML"
echo ""
echo "Restart Labwc (or reboot) for autostart and key bindings to take effect."
echo "To test immediately: python3 $CONFIG_DIR/launcher.py"