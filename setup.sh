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
    wlr-randr \
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

# Check if already installed (look for the shared library; the typelib may be in
# /usr/local which isn't in Python's default search path — launcher.py handles that)
GIR_OK=0
ldconfig -p 2>/dev/null | grep -q libgtk4-layer-shell && GIR_OK=1 || true

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
        -Dvapi=false \
        --prefix=/usr/local

    ninja -C build
    sudo ninja -C build install
    sudo ldconfig

    cd "$SCRIPT_DIR"

    # Verify — set GI_TYPELIB_PATH here the same way launcher.py does at runtime
    EXTRA_GI=$(find /usr/local/lib -maxdepth 3 -name 'Gtk4LayerShell-1.0.typelib' \
                    -exec dirname {} \; 2>/dev/null | head -1)
    GI_TYPELIB_PATH="${EXTRA_GI:+$EXTRA_GI:}${GI_TYPELIB_PATH:-}" \
    python3 -c "
import gi
gi.require_version('Gtk4LayerShell', '1.0')
from gi.repository import Gtk4LayerShell
print('    Gtk4LayerShell: OK')
" || {
        echo ""
        echo "ERROR: Gtk4LayerShell import still failing after build."
        TYPELIB=$(find /usr/local/lib -name 'Gtk4LayerShell-1.0.typelib' 2>/dev/null | head -1)
        if [ -n "$TYPELIB" ]; then
            TYPELIB_DIR=$(dirname "$TYPELIB")
            echo "Typelib found at: $TYPELIB"
            echo "Try: GI_TYPELIB_PATH=$TYPELIB_DIR python3 launcher.py"
        fi
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
# wlr-randr forces 1080p on the first HDMI output before the launcher starts.
# GDK_SCALE is not honoured by GTK4 on Wayland — the compositor owns scaling.
# RPi5 HDMI 0 = HDMI-A-1, HDMI 1 = HDMI-A-2. Adjust if your output differs
# (run `wlr-randr` inside a Labwc session to list connected outputs and modes).
AUTOSTART="$LABWC_DIR/autostart"
if ! grep -qF "wlr-randr" "$AUTOSTART" 2>/dev/null; then
    echo "wlr-randr --output HDMI-A-1 --mode 1920x1080" >> "$AUTOSTART"
    echo "    Added wlr-randr 1080p mode-set to $AUTOSTART"
else
    echo "    wlr-randr already in autostart, skipping."
fi
if ! grep -qF "$LAUNCHER" "$AUTOSTART" 2>/dev/null; then
    echo "python3 $LAUNCHER &" >> "$AUTOSTART"
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
EXTRA_GI=$(find /usr/local/lib -maxdepth 3 -name 'Gtk4LayerShell-1.0.typelib' \
                -exec dirname {} \; 2>/dev/null | head -1)
GI_TYPELIB_PATH="${EXTRA_GI:+$EXTRA_GI:}${GI_TYPELIB_PATH:-}" python3 - <<'PYCHECK'
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gtk, Gtk4LayerShell
print("  GTK4: OK")
print("  Gtk4LayerShell: OK")
PYCHECK

echo ""
echo "==> Setup complete!"
echo "    1. Edit $TV_DIR/apps.json to add your apps."
echo "    2. Log into a Labwc session (or reboot)."
echo "    3. Press the Super/Home key to open the launcher."
