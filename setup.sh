#!/usr/bin/env bash
# setup.sh — install couchpi and configure Labwc on Raspberry Pi OS (Bookworm or Trixie)
# Run as your normal user (NOT root). sudo is used only where needed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER="$SCRIPT_DIR/launcher.py"

LABWC_DIR="$HOME/.config/labwc"
TV_DIR="$HOME/.config/tv-launcher"

# ---------------------------------------------------------------------------
# 1. System packages (everything except gtk4-layer-shell, which isn't packaged
#    in Debian Trixie/Bookworm yet and must be built from source below)
# ---------------------------------------------------------------------------
echo "==> Installing system packages..."
sudo apt-get update -q
sudo apt-get install -y \
    python3-gi \
    python3-gi-cairo \
    gir1.2-gtk-4.0 \
    libgtk-4-dev \
    fonts-noto-color-emoji \
    python3-requests \
    labwc \
    git \
    meson \
    ninja-build \
    libwayland-dev \
    wayland-protocols \
    gobject-introspection \
    libgirepository1.0-dev \
    pkg-config

# ---------------------------------------------------------------------------
# 2. Build gtk4-layer-shell from source
#    gtk4-layer-shell is not yet in Raspberry Pi OS / Debian apt repos.
#    Source: https://github.com/wmww/gtk4-layer-shell
# ---------------------------------------------------------------------------

# Check if already installed and working
GIR_OK=0
python3 -c "
import gi
gi.require_version('GtkLayerShell', '0.1')
from gi.repository import GtkLayerShell
" 2>/dev/null && GIR_OK=1 || true

if [ "$GIR_OK" -eq 1 ]; then
    echo "==> gtk4-layer-shell already installed, skipping build."
else
    echo "==> Building gtk4-layer-shell from source (this takes ~2 minutes)..."

    BUILD_DIR="$(mktemp -d)"
    trap 'rm -rf "$BUILD_DIR"' EXIT

    git clone --depth=1 https://github.com/wmww/gtk4-layer-shell.git "$BUILD_DIR/gtk4-layer-shell"
    cd "$BUILD_DIR/gtk4-layer-shell"

    meson setup build \
        -Dexamples=false \
        -Ddocs=false \
        -Dtests=false \
        --prefix=/usr/local

    ninja -C build
    sudo ninja -C build install
    sudo ldconfig

    # On some systems the typelib ends up in /usr/local/lib/<arch>/girepository-1.0/
    # but Python's gi only searches /usr/lib. Add a symlink to bridge the gap.
    TYPELIB=$(find /usr/local/lib -name 'GtkLayerShell-0.1.typelib' 2>/dev/null | head -1)
    if [ -n "$TYPELIB" ]; then
        GI_SYSTEM_DIR=$(python3 -c "
import gi, os
# gi searches these paths for typelibs
search_paths = gi.get_overrides_search_path() if hasattr(gi, 'get_overrides_search_path') else []
# fall back to the standard system path
print('/usr/lib/$(dpkg-architecture -qDEB_HOST_MULTIARCH 2>/dev/null || echo aarch64-linux-gnu)/girepository-1.0')
")
        sudo mkdir -p "$GI_SYSTEM_DIR"
        sudo ln -sf "$TYPELIB" "$GI_SYSTEM_DIR/GtkLayerShell-0.1.typelib"
        echo "    Linked typelib: $TYPELIB -> $GI_SYSTEM_DIR/GtkLayerShell-0.1.typelib"
    fi

    cd "$SCRIPT_DIR"

    # Verify
    python3 -c "
import gi
gi.require_version('GtkLayerShell', '0.1')
from gi.repository import GtkLayerShell
print('    GtkLayerShell: OK')
" || {
        echo ""
        echo "ERROR: GtkLayerShell import still failing after build."
        echo "Try running: sudo ldconfig && python3 -c \"import gi; gi.require_version('GtkLayerShell','0.1'); from gi.repository import GtkLayerShell\""
        echo "If the typelib is in /usr/local/lib/.../girepository-1.0/, set:"
        echo "  export GI_TYPELIB_PATH=/usr/local/lib/\$(gcc -dumpmachine)/girepository-1.0"
        echo "and add that line to ~/.bash_profile."
        exit 1
    }
fi

# ---------------------------------------------------------------------------
# 3. Launcher config directory
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
# 4. Labwc config directory
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
# 5. Make launcher executable
# ---------------------------------------------------------------------------
chmod +x "$LAUNCHER"

# ---------------------------------------------------------------------------
# 6. Final verification
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
