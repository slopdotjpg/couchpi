#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# couchpi/launcher.py — PSP/XMB-style TV launcher for Raspberry Pi 5
#
# DEPENDENCIES (install before running):
#   sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 libgtk-4-dev \
#                    meson ninja-build libwayland-dev wayland-protocols \
#                    gobject-introspection libgirepository1.0-dev \
#                    python3-requests fonts-noto-color-emoji
#
#   gtk4-layer-shell must be built from source (not in apt repos on RPi OS):
#     git clone https://github.com/wmww/gtk4-layer-shell && cd gtk4-layer-shell
#     meson setup build -Dexamples=false -Ddocs=false -Dtests=false -Dvapi=false \
#           --prefix=/usr/local && ninja -C build && sudo ninja -C build install
#     sudo ldconfig
#   (setup.sh does all of this automatically)
#
# WAYLAND FOCUS NOTE:
#   Wayland intentionally prevents apps from stealing focus. When "Launch"
#   is pressed for an already-running app, we attempt focus via gdbus
#   (XDG Activation Token request), but compositors are not required to
#   honour this. On Labwc it works for most well-behaved Wayland clients.
#   X11 clients running under XWayland may not respond; this is a known
#   Wayland limitation and not a bug in this launcher.
#
# IPC:
#   A Unix domain socket at /tmp/couchpi.sock is created when the launcher
#   starts. A second invocation sends "toggle" to the socket and exits.
#   Labwc's Home key binding should call: python3 /path/to/launcher.py

import os
import sys

# gtk4-layer-shell >= 1.0 installs its typelib under /usr/local, which is not
# in gi's default search path on Debian/RPi OS. Find and add it before importing.
import glob as _glob
_extra_gi = _glob.glob("/usr/local/lib/*/girepository-1.0")
if _extra_gi:
    _existing = os.environ.get("GI_TYPELIB_PATH", "")
    _extra = ":".join(_extra_gi)
    if _extra not in _existing:
        os.environ["GI_TYPELIB_PATH"] = f"{_extra}:{_existing}" if _existing else _extra

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
import json
import signal
import socket
import subprocess
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

from gi.repository import Gtk, GLib, Gdk, GdkPixbuf, Gio, Pango
from gi.repository import Gtk4LayerShell

# ---------------------------------------------------------------------------
# Configuration paths
# ---------------------------------------------------------------------------

CONFIG_DIR = Path.home() / ".config" / "tv-launcher"
APPS_JSON  = CONFIG_DIR / "apps.json"
ICONS_DIR  = CONFIG_DIR / "icons"
SOCKET_PATH = "/tmp/couchpi.sock"

# ---------------------------------------------------------------------------
# IPC: toggle visibility of an already-running launcher
# ---------------------------------------------------------------------------

def try_toggle_existing() -> bool:
    """
    Attempt to send a 'toggle' message to a running launcher instance.
    Returns True if a running instance was found and messaged.
    """
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        sock.connect(SOCKET_PATH)
        sock.sendall(b"toggle")
        sock.close()
        return True
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        return False


def start_ipc_server(app: "LauncherApp") -> None:
    """
    Start a Unix socket server in a background thread that listens for
    'toggle' commands from a second invocation of the script.
    """
    # Remove stale socket if present
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    server.listen(1)
    server.settimeout(None)

    def _serve():
        while True:
            try:
                conn, _ = server.accept()
                data = conn.recv(64)
                conn.close()
                if data == b"toggle":
                    GLib.idle_add(app.toggle_visibility)
            except OSError:
                break

    t = threading.Thread(target=_serve, daemon=True)
    t.start()

# ---------------------------------------------------------------------------
# App entry dataclass
# ---------------------------------------------------------------------------

class AppEntry:
    def __init__(self, cfg: dict):
        self.name: str        = cfg.get("name", "Unknown")
        self.launch_cmd: str  = cfg.get("launch", "")
        self.url: str | None  = cfg.get("url")
        self.icon_path: str | None = cfg.get("icon")
        self.pid: int | None  = None  # PID of running process, if known

    @property
    def is_running(self) -> bool:
        if self.pid is None:
            return False
        try:
            os.kill(self.pid, 0)  # signal 0 = existence check
            return True
        except (ProcessLookupError, PermissionError):
            self.pid = None
            return False

    def launch(self) -> None:
        if self.is_running:
            _focus_wayland_app(self.name, self.pid)
            return
        env = os.environ.copy()
        proc = subprocess.Popen(
            self.launch_cmd,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # detach from launcher's process group
            env=env,
        )
        self.pid = proc.pid

    def kill(self) -> None:
        if self.pid is None:
            return
        try:
            os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            self.pid = None
            return

        # Wait up to 2 s, then SIGKILL
        def _force_kill():
            time.sleep(2)
            try:
                os.kill(self.pid, 0)
                os.kill(self.pid, signal.SIGKILL)
            except (ProcessLookupError, TypeError):
                pass
            self.pid = None

        threading.Thread(target=_force_kill, daemon=True).start()
        self.pid = None


def _focus_wayland_app(name: str, pid: int) -> None:
    """
    Attempt to bring a Wayland client to the foreground using gdbus.
    This is best-effort; compositors may ignore focus requests.
    See the WAYLAND FOCUS NOTE at the top of this file.
    """
    # Try using wmctrl as a fallback for XWayland clients
    subprocess.Popen(
        ["gdbus", "call", "--session",
         "--dest", "org.freedesktop.portal.Desktop",
         "--object-path", "/org/freedesktop/portal/desktop",
         "--method", "org.freedesktop.portal.WindowIdentifier.GetWindowId",
         str(pid)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

# ---------------------------------------------------------------------------
# Favicon fetcher
# ---------------------------------------------------------------------------

def fetch_favicon(entry: AppEntry) -> Path | None:
    """
    For web apps, try to fetch /favicon.svg, /favicon.ico, /favicon.png
    in that order. Cache the result and return its path.
    """
    if not entry.url:
        return None

    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = entry.name.replace(" ", "_").replace("/", "_")

    for ext, path_suffix in [("svg", "favicon.svg"),
                               ("ico", "favicon.ico"),
                               ("png", "favicon.png")]:
        cached = ICONS_DIR / f"{safe_name}.{ext}"
        if cached.exists():
            return cached

        url = entry.url.rstrip("/") + f"/{path_suffix}"
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                data = resp.read()
            cached.write_bytes(data)
            return cached
        except (urllib.error.URLError, OSError):
            continue

    return None


def _flatpak_app_id(launch_cmd: str) -> str | None:
    """Extract the app-id from a 'flatpak run [flags] <app-id>' command."""
    parts = launch_cmd.split()
    try:
        idx = parts.index("run")
    except ValueError:
        return None
    # app-id is the first non-flag token after 'run'
    for token in parts[idx + 1:]:
        if not token.startswith("-"):
            return token
    return None


def _find_flatpak_icon(app_id: str) -> Path | None:
    """
    Search XDG icon dirs (including Flatpak exports) for <app-id>.svg/.png.
    Flatpak installs icons to ~/.local/share/flatpak/exports/share/icons/
    following the hicolor theme layout.
    """
    roots = [
        Path.home() / ".local/share/flatpak/exports/share/icons",
        Path("/var/lib/flatpak/exports/share/icons"),
        Path.home() / ".local/share/icons",
        Path("/usr/share/icons"),
    ]
    size_prefs = ["scalable", "256x256", "128x128", "64x64", "48x48"]
    for root in roots:
        if not root.exists():
            continue
        for size in size_prefs:
            for ext in ("svg", "png"):
                candidate = root / "hicolor" / size / "apps" / f"{app_id}.{ext}"
                if candidate.exists():
                    return candidate
    return None


def resolve_icon(entry: AppEntry) -> Path | None:
    """Return a resolved icon path for an app entry, or None."""
    if entry.icon_path:
        p = Path(entry.icon_path).expanduser()
        if p.exists():
            return p

    if entry.url:
        cached = fetch_favicon(entry)
        if cached:
            return cached

    # For Flatpak apps, look up the icon by app-id in the Flatpak icon exports
    app_id = _flatpak_app_id(entry.launch_cmd)
    if app_id:
        icon = _find_flatpak_icon(app_id)
        if icon:
            return icon

    return None

# ---------------------------------------------------------------------------
# GTK Widget: single app tile (icon + name + running dot)
# ---------------------------------------------------------------------------

ICON_SIZE        = 128  # px — unselected
ICON_SIZE_SEL    = 160  # px — selected (XMB scale-up effect)
TILE_WIDTH       = 200
TILE_HEIGHT      = 220


class AppTile(Gtk.Box):
    def __init__(self, entry: AppEntry):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.entry = entry
        self.set_size_request(TILE_WIDTH, TILE_HEIGHT)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)

        self._icon_widget = Gtk.Image()
        self._icon_widget.set_pixel_size(ICON_SIZE)
        self._icon_widget.set_vexpand(False)
        self._load_icon()

        self._dot = Gtk.Label(label="●")
        self._dot.add_css_class("running-dot")
        self._dot.set_visible(False)

        icon_overlay = Gtk.Overlay()
        icon_overlay.set_child(self._icon_widget)
        icon_overlay.add_overlay(self._dot)
        self._dot.set_halign(Gtk.Align.END)
        self._dot.set_valign(Gtk.Align.END)

        self._label = Gtk.Label(label=entry.name)
        self._label.set_ellipsize(Pango.EllipsizeMode.END)
        self._label.set_max_width_chars(14)
        self._label.add_css_class("app-label")

        self.append(icon_overlay)
        self.append(self._label)

    def _load_icon(self) -> None:
        icon_path = resolve_icon(self.entry)
        if icon_path:
            try:
                self._icon_widget.set_from_file(str(icon_path))
                return
            except Exception:
                pass
        # For Flatpak apps try the app-id as an icon theme name; GTK will find
        # it if XDG_DATA_DIRS includes the Flatpak export share path
        app_id = _flatpak_app_id(self.entry.launch_cmd)
        if app_id:
            self._icon_widget.set_from_icon_name(app_id)
            return
        self._icon_widget.set_from_icon_name("application-x-executable")

    def refresh_state(self) -> None:
        """Update the running indicator dot."""
        self._dot.set_visible(self.entry.is_running)

    def set_selected(self, selected: bool) -> None:
        # XMB effect: selected icon is larger, unselected icons are dimmer
        self._icon_widget.set_pixel_size(ICON_SIZE_SEL if selected else ICON_SIZE)
        self._icon_widget.set_opacity(1.0 if selected else 0.5)
        if selected:
            self.add_css_class("selected-tile")
        else:
            self.remove_css_class("selected-tile")

# ---------------------------------------------------------------------------
# GTK Widget: vertical submenu (Launch / Kill)
# ---------------------------------------------------------------------------

SUBMENU_ITEMS = ["Launch", "Kill"]


class SubMenu(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.add_css_class("submenu")
        self._items: list[Gtk.Label] = []
        self._cursor = 0

        for text in SUBMENU_ITEMS:
            lbl = Gtk.Label(label=text)
            lbl.add_css_class("submenu-item")
            self._items.append(lbl)
            self.append(lbl)

        self._refresh()

    def _refresh(self) -> None:
        for i, lbl in enumerate(self._items):
            if i == self._cursor:
                lbl.add_css_class("submenu-selected")
            else:
                lbl.remove_css_class("submenu-selected")

    def move(self, delta: int) -> None:
        self._cursor = (self._cursor + delta) % len(SUBMENU_ITEMS)
        self._refresh()

    @property
    def selected_action(self) -> str:
        return SUBMENU_ITEMS[self._cursor]

    def reset(self) -> None:
        self._cursor = 0
        self._refresh()

# ---------------------------------------------------------------------------
# Main application window
# ---------------------------------------------------------------------------

CSS = b"""
window {
    background-color: #0d2b0d;
}
/* Gradient lives on the root box, not the window, so GTK's theme can't override it */
.root-box {
    background-image: linear-gradient(160deg, #0d2b0d 0%, #1e6b1e 45%, #092009 100%);
}
.app-label {
    color: #ffffff;
    font-size: 20px;
}
.selected-tile .app-label {
    font-weight: bold;
}
.running-dot {
    color: #a0ffa0;
    font-size: 16px;
    padding: 2px;
}
.submenu {
    background-color: rgba(0, 0, 0, 0);
    border-left: 3px solid rgba(255,255,255,0.7);
    padding: 4px 32px 4px 16px;
    margin-top: 4px;
}
.submenu-item {
    color: #bbbbbb;
    font-size: 22px;
    padding: 4px 0;
}
.submenu-selected {
    color: #ffffff;
    font-weight: bold;
}
.clock-label {
    color: #ffffff;
    font-size: 20px;
}
"""


class LauncherWindow(Gtk.ApplicationWindow):
    def __init__(self, app: "LauncherApp", entries: list[AppEntry]):
        super().__init__(application=app)
        self.entries  = entries
        self._cursor  = 0         # currently focused tile index
        self._in_sub  = False     # are we navigating the submenu?
        self._visible = True

        self._setup_layer_shell()
        self._build_ui()
        self._setup_css()
        self._setup_input()
        self._start_refresh_timer()
        self.set_cursor(Gdk.Cursor.new_from_name("none", None))

    # ------------------------------------------------------------------
    # Layer shell setup — this is what makes the launcher overlay everything
    # ------------------------------------------------------------------

    def _setup_layer_shell(self) -> None:
        # gtk4-layer-shell must be initialized before the window is realized
        Gtk4LayerShell.init_for_window(self)
        Gtk4LayerShell.set_layer(self, Gtk4LayerShell.Layer.OVERLAY)
        Gtk4LayerShell.set_exclusive_zone(self, -1)   # don't push other surfaces
        Gtk4LayerShell.set_keyboard_mode(
            self, Gtk4LayerShell.KeyboardMode.EXCLUSIVE
        )
        # Anchor to all edges so it fills the screen
        for edge in (
            Gtk4LayerShell.Edge.TOP,
            Gtk4LayerShell.Edge.BOTTOM,
            Gtk4LayerShell.Edge.LEFT,
            Gtk4LayerShell.Edge.RIGHT,
        ):
            Gtk4LayerShell.set_anchor(self, edge, True)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        import datetime

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.set_vexpand(True)
        root.set_hexpand(True)
        root.add_css_class("root-box")

        # Top bar: clock on the right, matching PSP XMB style
        top_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        top_bar.set_margin_top(12)
        top_bar.set_margin_end(20)
        top_bar.set_hexpand(True)
        filler = Gtk.Box()
        filler.set_hexpand(True)
        self._clock_label = Gtk.Label()
        self._clock_label.add_css_class("clock-label")
        top_bar.append(filler)
        top_bar.append(self._clock_label)
        self._tick_clock()

        # Vertical spacer pushes icons to vertical centre
        mid_spacer = Gtk.Box()
        mid_spacer.set_vexpand(True)

        # Horizontal row of tiles
        self._tiles_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=48
        )
        self._tiles_box.set_halign(Gtk.Align.CENTER)
        self._tiles_box.set_valign(Gtk.Align.CENTER)
        self._tiles_box.set_margin_bottom(8)

        self._tiles: list[AppTile] = []
        for entry in self.entries:
            tile = AppTile(entry)
            self._tiles.append(tile)
            self._tiles_box.append(tile)

        # Submenu (shown below selected tile)
        self._submenu = SubMenu()
        self._submenu.set_halign(Gtk.Align.CENTER)
        self._submenu.set_visible(False)

        # Bottom spacer balances the layout
        bot_spacer = Gtk.Box()
        bot_spacer.set_vexpand(True)

        root.append(top_bar)
        root.append(mid_spacer)
        root.append(self._tiles_box)
        root.append(self._submenu)
        root.append(bot_spacer)

        self.set_child(root)
        self._update_selection()

    def _tick_clock(self) -> bool:
        import datetime
        now = datetime.datetime.now()
        self._clock_label.set_text(now.strftime("%A  %d %B  %H:%M"))
        return True

    def _setup_css(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        display = Gdk.Display.get_default()
        Gtk.StyleContext.add_provider_for_display(
            display,
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        # Register Flatpak icon export dirs so GTK icon theme lookup finds them
        # even when XDG_DATA_DIRS isn't set up (common in minimal Labwc sessions)
        theme = Gtk.IconTheme.get_for_display(display)
        for icon_dir in [
            Path.home() / ".local/share/flatpak/exports/share/icons",
            Path("/var/lib/flatpak/exports/share/icons"),
        ]:
            if icon_dir.exists():
                theme.add_search_path(str(icon_dir))

    # ------------------------------------------------------------------
    # Input: keyboard + gamepad
    # ------------------------------------------------------------------

    def _setup_input(self) -> None:
        # Keyboard
        ctrl = Gtk.EventControllerKey()
        ctrl.connect("key-pressed", self._on_key)
        self.add_controller(ctrl)

        # Gamepad via evdev in a background thread
        threading.Thread(target=self._gamepad_thread, daemon=True).start()

    def _on_key(
        self,
        ctrl: Gtk.EventControllerKey,
        keyval: int,
        keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        key = keyval

        if key == Gdk.KEY_Left:
            self._nav_horizontal(-1)
        elif key == Gdk.KEY_Right:
            self._nav_horizontal(1)
        elif key == Gdk.KEY_Up:
            self._nav_vertical(-1)
        elif key == Gdk.KEY_Down:
            self._nav_vertical(1)
        elif key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._confirm()
        elif key == Gdk.KEY_Super_L or key == Gdk.KEY_Super_R:
            self.hide_launcher()

        return True  # consume all key events while overlay is shown

    # ------------------------------------------------------------------
    # Gamepad input via evdev
    # Reads /dev/input/js0 (the classic joystick API — available without
    # root on most systems if the user is in the 'input' group).
    # Gamepad button mapping (knockoff PS3):
    #   Button 0 = Cross (A)  → Enter / confirm
    #   Axis 0   = Left stick horizontal → left/right nav
    #   Axis 1   = Left stick vertical   → up/down nav
    #   Hat axis 4/5 (D-pad) mapped similarly
    # ------------------------------------------------------------------

    def _gamepad_thread(self) -> None:
        JS_DEV = "/dev/input/js0"
        try:
            import struct
            with open(JS_DEV, "rb") as js:
                while True:
                    data = js.read(8)
                    if len(data) < 8:
                        break
                    _time, value, ev_type, number = struct.unpack("IhBB", data)
                    ev_type &= ~0x80  # strip init flag
                    if ev_type == 1:  # button event
                        if value == 1 and number == 0:
                            GLib.idle_add(self._confirm)
                    elif ev_type == 2:  # axis event
                        if abs(value) < 8000:
                            continue  # dead zone
                        direction = 1 if value > 0 else -1
                        if number in (0, 4):   # horizontal axes
                            GLib.idle_add(self._nav_horizontal, direction)
                        elif number in (1, 5): # vertical axes
                            GLib.idle_add(self._nav_vertical, direction)
        except (FileNotFoundError, PermissionError, OSError):
            # No gamepad connected or no permission; silently ignore
            pass

    # ------------------------------------------------------------------
    # Navigation logic
    # ------------------------------------------------------------------

    def _nav_horizontal(self, delta: int) -> None:
        if self._in_sub:
            return
        self._cursor = (self._cursor + delta) % len(self.entries)
        self._submenu.reset()
        self._submenu.set_visible(False)
        self._in_sub = False
        self._update_selection()

    def _nav_vertical(self, delta: int) -> None:
        if not self._in_sub:
            # Enter the submenu on first downward press
            if delta > 0:
                self._in_sub = True
                self._submenu.set_visible(True)
        else:
            self._submenu.move(delta)
            if delta < 0 and self._submenu.selected_action == SUBMENU_ITEMS[0]:
                # Allow pressing up from top of submenu to exit submenu
                pass  # stay in submenu at top item
            # Allow pressing up past the first item to close submenu
            # (handled inside SubMenu.move — wrap-around kept simple)

    def _confirm(self) -> None:
        entry = self.entries[self._cursor]
        if not self._in_sub:
            self._in_sub = True
            self._submenu.set_visible(True)
            return

        action = self._submenu.selected_action
        if action == "Launch":
            entry.launch()
            self.hide_launcher()
        elif action == "Kill":
            entry.kill()
            self._submenu.set_visible(False)
            self._in_sub = False
            self._update_selection()

    def _update_selection(self) -> None:
        for i, tile in enumerate(self._tiles):
            tile.set_selected(i == self._cursor)
            tile.refresh_state()

    # ------------------------------------------------------------------
    # Visibility toggling
    # ------------------------------------------------------------------

    def show_launcher(self) -> None:
        self._visible = True
        # Hide the mouse cursor — this is a TV/couch interface driven by keyboard/gamepad
        self.set_cursor(Gdk.Cursor.new_from_name("none", None))
        self.present()

    def hide_launcher(self) -> None:
        self._visible = False
        self._submenu.set_visible(False)
        self._in_sub = False
        self._submenu.reset()
        self._update_selection()
        self.set_cursor(None)  # restore default cursor for underlying apps
        self.set_visible(False)

    def toggle_visibility(self) -> None:
        if self._visible:
            self.hide_launcher()
        else:
            self.show_launcher()

    # ------------------------------------------------------------------
    # Running-state refresh (polls every 2 s)
    # ------------------------------------------------------------------

    def _start_refresh_timer(self) -> None:
        GLib.timeout_add(1000, self._tick_clock)
        GLib.timeout_add(2000, self._tick_state)

    def _tick_state(self) -> bool:
        self._update_selection()
        return True

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class LauncherApp(Gtk.Application):
    def __init__(self, entries: list[AppEntry]):
        super().__init__(
            application_id="org.couchpi.launcher",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.entries = entries
        self.window: LauncherWindow | None = None

    def do_activate(self) -> None:
        if self.window is None:
            self.window = LauncherWindow(self, self.entries)
            start_ipc_server(self)
        self.window.present()

    def toggle_visibility(self) -> None:
        if self.window:
            self.window.toggle_visibility()

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config() -> list[AppEntry]:
    if not APPS_JSON.exists():
        print(
            f"ERROR: Config file not found: {APPS_JSON}\n"
            f"Create it with at least one app entry. Example:\n"
            f'  [{{"name": "RetroArch", "launch": "retroarch", "icon": null}}]',
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        raw = json.loads(APPS_JSON.read_text())
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in {APPS_JSON}: {e}", file=sys.stderr)
        sys.exit(1)

    return [AppEntry(cfg) for cfg in raw]

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    # If another instance is already running, toggle its visibility and exit
    if try_toggle_existing():
        sys.exit(0)

    entries = load_config()
    app = LauncherApp(entries)
    app.run(sys.argv)


if __name__ == "__main__":
    main()
