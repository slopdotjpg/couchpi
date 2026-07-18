#!/usr/bin/env bash
# setup.sh — install couchpi and configure Labwc on Raspberry Pi OS Bookworm
# Run as your normal user (NOT root). sudo is used only where needed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER="$SCRIPT_DIR/launcher.py"

LABWC_DIR="$HOME/.config/labwc"
TV_DIR="$HOME/.config/tv-launcher"

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------
echo "==> Installing system packages..."
sudo apt-get update -q
sudo apt-get install -y \
    python3-gi \
    python3-gi-cairo \
    gir1.2-gtk-4.0 \
    libgtk-4-dev \
    gtk4-layer-shell \
    gir1.2-gtk4-layer-shell \
    fonts-noto-color-emoji \
    python3-requests \
    labwc

# gtk4-layer-shell GIR name differs by distro; try both
GIR_OK=0
python3 -c "
import gi
gi.require_version('GtkLayerShell', '0.1')
from gi.repository import GtkLayerShell
print('GtkLayerShell GIR: OK')
" && GIR_OK=1 || true

if [ "$GIR_OK" -eq 0 ]; then
    echo "WARNING: GtkLayerShell GIR typelib not found via apt."
    echo "         You may need to build gtk4-layer-shell from source:"
    echo "         https://github.com/wmww/gtk4-layer-shell"
fi

# ---------------------------------------------------------------------------
# 2. Launcher config directory
# ---------------------------------------------------------------------------
echo "==> Setting up ~/.config/tv-launcher/ ..."
mkdir -p "$TV_DIR/icons"

if [ ! -f "$TV_DIR/apps.json" ]; then
    cp "$SCRIPT_DIR/apps.json.example" "$TV_DIR/apps.json"
    echo "    Copied example apps.json — edit $TV_DIR/apps.json to add your apps."
else
    echo "    apps.json already exists, leaving it alone."
fi

# ---------------------------------------------------------------------------
# 3. Labwc config directory
# ---------------------------------------------------------------------------
echo "==> Configuring Labwc..."
mkdir -p "$LABWC_DIR"

# --- autostart ---
AUTOSTART="$LABWC_DIR/autostart"
AUTOSTART_LINE="python3 $LAUNCHER &"
if ! grep -qF "$LAUNCHER" "$AUTOSTART" 2>/dev/null; then
    echo "$AUTOSTART_LINE" >> "$AUTOSTART"
    echo "    Added launcher to $AUTOSTART"
else
    echo "    Launcher already in autostart, skipping."
fi

# --- rc.xml: Home key binding ---
RC="$LABWC_DIR/rc.xml"

if [ ! -f "$RC" ]; then
    # Write a minimal rc.xml with just the keybind
    cat > "$RC" <<'RCXML'
<?xml version="1.0" encoding="UTF-8"?>
<openbox_config xmlns="http://openbox.org/3.4/rc">
  <keyboard>
    <keybind key="Super_L">
      <action name="Execute">
        <command>python3 LAUNCHER_PLACEHOLDER</command>
      </action>
    </keybind>
  </keyboard>
</openbox_config>
RCXML
    sed -i "s|LAUNCHER_PLACEHOLDER|$LAUNCHER|g" "$RC"
    echo "    Created $RC with Home key binding."
else
    # Check if keybind already present
    if ! grep -qF "$LAUNCHER" "$RC"; then
        echo ""
        echo "    NOTICE: $RC already exists."
        echo "    Add the following keybind inside the <keyboard> section manually:"
        echo ""
        echo "    <keybind key=\"Super_L\">"
        echo "      <action name=\"Execute\">"
        echo "        <command>python3 $LAUNCHER</command>"
        echo "      </action>"
        echo "    </keybind>"
        echo ""
    else
        echo "    Keybind already present in $RC, skipping."
    fi
fi

# ---------------------------------------------------------------------------
# 4. Make launcher executable
# ---------------------------------------------------------------------------
chmod +x "$LAUNCHER"

# ---------------------------------------------------------------------------
# 5. Verify
# ---------------------------------------------------------------------------
echo ""
echo "==> Verifying Python imports..."
python3 - <<'PYCHECK'
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("GtkLayerShell", "0.1")
from gi.repository import Gtk, GtkLayerShell
print("  GTK4: OK")
print("  GtkLayerShell: OK")
PYCHECK

echo ""
echo "==> Setup complete!"
echo "    1. Edit $TV_DIR/apps.json to add your apps."
echo "    2. Log into a Labwc session (or reboot)."
echo "    3. Press the Super/Home key to open the launcher."
