#!/usr/bin/env python3
# couchpi — fullscreen TV launcher for Raspberry Pi 5 + Labwc (Wayland)
#
# Dependencies:
#   apt: python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-gtklayershell-0.1 python3-evdev
#   pip: (none — stdlib + gi only)
#
# IPC: Unix socket at ~/.config/couchpi/couchpi.sock
# Config: ~/.config/couchpi/apps.json
# CSS:    ~/.config/couchpi/style.css
# PIDs:   ~/.config/couchpi/pids.json
# Icons:  ~/.config/couchpi/icons/

import gi
gi.require_version("Gtk", "4.0")

# gtk4-layer-shell must be required before the window is created
try:
    gi.require_version("GtkLayerShell", "1.0")
    from gi.repository import GtkLayerShell
    HAS_LAYER_SHELL = True
except (ValueError, ImportError):
    HAS_LAYER_SHELL = False

from gi.repository import Gtk, Gdk, GLib, GdkPixbuf, Gio
import sys
import os
import json
import time
import signal
import socket
import threading
import subprocess
import urllib.request
import urllib.parse
import urllib.error
import re

# ---------------------------------------------------------------------------
# Sanity checks before anything else
# ---------------------------------------------------------------------------

if not os.environ.get("WAYLAND_DISPLAY"):
    print(
        "ERROR: WAYLAND_DISPLAY is not set. couchpi must be run inside a Wayland session.",
        file=sys.stderr,
    )
    sys.exit(1)

if not HAS_LAYER_SHELL:
    print(
        "ERROR: GtkLayerShell 1.0 is not available.\n"
        "Install it with: sudo apt install gir1.2-gtklayershell-0.1",
        file=sys.stderr,
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

CONFIG_DIR = os.path.expanduser("~/.config/couchpi")
APPS_JSON = os.path.join(CONFIG_DIR, "apps.json")
PIDS_JSON = os.path.join(CONFIG_DIR, "pids.json")
STYLE_CSS = os.path.join(CONFIG_DIR, "style.css")
ICONS_DIR = os.path.join(CONFIG_DIR, "icons")
SOCKET_PATH = os.path.join(CONFIG_DIR, "couchpi.sock")

os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(ICONS_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Default configs written on first run
# ---------------------------------------------------------------------------

DEFAULT_APPS = [
    {
        "name": "RetroArch",
        "launch": "retroarch",
        "icon": None,
    },
    {
        "name": "Jellyfin",
        "url": "http://localhost:8096",
        "launch": "chromium --kiosk http://localhost:8096",
        "icon": None,
    },
]

DEFAULT_CSS = """\
/* couchpi style — PS3 XMB inspired */

window {
    background: linear-gradient(to bottom, #00194a, #001030);
}

.clock {
    color: white;
    font-size: 20px;
    font-family: Inter, sans-serif;
    font-weight: 300;
    padding: 16px 24px;
}

.app-label {
    color: white;
    font-size: 18px;
    font-family: Inter, sans-serif;
    font-weight: 400;
}

.icon-selected {
    opacity: 1.0;
}

.icon-unselected {
    opacity: 0.6;
}

.running-dot {
    color: white;
    font-size: 10px;
}

.submenu-box {
    background-color: transparent;
    padding: 4px 0;
}

.submenu-item {
    color: white;
    font-size: 16px;
    font-family: Inter, sans-serif;
    padding: 6px 20px;
    border-radius: 20px;
}

.submenu-item-selected {
    background-color: rgba(255, 255, 255, 0.18);
    color: white;
    font-size: 16px;
    font-family: Inter, sans-serif;
    padding: 6px 20px;
    border-radius: 20px;
}
"""

FALLBACK_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96">
  <rect width="96" height="96" rx="16" fill="#1a2a4a"/>
  <rect x="16" y="16" width="26" height="26" rx="4" fill="white" opacity="0.8"/>
  <rect x="54" y="16" width="26" height="26" rx="4" fill="white" opacity="0.8"/>
  <rect x="16" y="54" width="26" height="26" rx="4" fill="white" opacity="0.8"/>
  <rect x="54" y="54" width="26" height="26" rx="4" fill="white" opacity="0.8"/>
</svg>
"""

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def ensure_default_apps():
    if not os.path.exists(APPS_JSON):
        with open(APPS_JSON, "w") as f:
            json.dump(DEFAULT_APPS, f, indent=2)

def ensure_default_css():
    if not os.path.exists(STYLE_CSS):
        with open(STYLE_CSS, "w") as f:
            f.write(DEFAULT_CSS)

def load_apps():
    ensure_default_apps()
    with open(APPS_JSON) as f:
        return json.load(f)

def load_pids():
    if not os.path.exists(PIDS_JSON):
        return {}
    with open(PIDS_JSON) as f:
        return json.load(f)

def save_pids(pids):
    with open(PIDS_JSON, "w") as f:
        json.dump(pids, f, indent=2)

def clean_pids(pids):
    """Remove stale PIDs for processes that are no longer alive."""
    alive = {}
    for name, pid in pids.items():
        try:
            os.kill(pid, 0)
            alive[name] = pid
        except (ProcessLookupError, PermissionError):
            pass
    return alive

# ---------------------------------------------------------------------------
# IPC: Unix socket server (runs in a background thread)
# ---------------------------------------------------------------------------

def start_ipc_server(toggle_callback):
    """Listen on SOCKET_PATH. Each connection triggers toggle_callback on the main thread."""
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    server.listen(1)

    def _serve():
        while True:
            try:
                conn, _ = server.accept()
                conn.close()
                # Schedule the toggle on the GTK main thread
                GLib.idle_add(toggle_callback)
            except Exception:
                break

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    return server

def send_toggle():
    """Connect to the running instance's socket to trigger a toggle."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(SOCKET_PATH)
        s.close()
        return True
    except (FileNotFoundError, ConnectionRefusedError):
        return False

# ---------------------------------------------------------------------------
# Favicon fetching for web apps
# ---------------------------------------------------------------------------

def sanitize_name(name):
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name).lower()

def fetch_favicon(app):
    """Try to download a favicon for a web app. Returns local path or None."""
    url = app.get("url")
    if not url:
        return None

    name = sanitize_name(app.get("name", "app"))
    parsed = urllib.parse.urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    candidates = [
        (f"{base}/favicon.svg", ".svg"),
        (f"{base}/favicon.ico", ".ico"),
        (f"{base}/favicon.png", ".png"),
    ]

    for fav_url, ext in candidates:
        dest = os.path.join(ICONS_DIR, f"{name}{ext}")
        if os.path.exists(dest):
            return dest
        try:
            req = urllib.request.Request(fav_url, headers={"User-Agent": "couchpi/1.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = resp.read()
            with open(dest, "wb") as f:
                f.write(data)
            return dest
        except Exception:
            continue
    return None

def get_icon_path(app):
    """Resolve icon path for an app, fetching favicon if needed."""
    icon = app.get("icon")
    if icon:
        return os.path.expanduser(icon)
    if app.get("url"):
        cached = fetch_favicon(app)
        if cached:
            return cached
    return None

def load_pixbuf(path, size):
    """Load an image as a GdkPixbuf scaled to size×size. Returns None on failure."""
    if path is None:
        return None
    try:
        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size, size, True)
        return pb
    except Exception:
        return None

def make_fallback_texture(size):
    """Create a fallback texture from the inline SVG."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp:
        tmp.write(FALLBACK_SVG.encode())
        tmp_path = tmp.name
    try:
        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(tmp_path, size, size, True)
    except Exception:
        pb = None
    finally:
        os.unlink(tmp_path)
    return pb

# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------

class ProcessManager:
    def __init__(self):
        self.pids = clean_pids(load_pids())
        save_pids(self.pids)

    def is_running(self, name):
        pid = self.pids.get(name)
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            del self.pids[name]
            save_pids(self.pids)
            return False

    def launch(self, app):
        """Launch app as a detached subprocess."""
        name = app["name"]
        cmd = app.get("launch", "")
        if not cmd:
            return

        # Wayland focus stealing is intentionally restricted by the protocol.
        # If the app is already running we attempt to raise it via gdbus, but
        # this is best-effort — many apps ignore or don't implement the interface.
        if self.is_running(name):
            pid = self.pids[name]
            print(f"[couchpi] {name} already running (PID {pid}), focus request sent (may be ignored by Wayland)")
            # Attempt XDG activation via compositor — may silently fail
            token_cmd = [
                "gdbus", "call", "--session",
                "--dest", "org.freedesktop.portal.Desktop",
                "--object-path", "/org/freedesktop/portal/desktop",
                "--method", "org.freedesktop.portal.Activationtoken.RequestToken",
                name, {}
            ]
            subprocess.Popen(token_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return

        try:
            proc = subprocess.Popen(
                cmd,
                shell=True,
                start_new_session=True,  # detach from launcher's process group
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.pids[name] = proc.pid
            save_pids(self.pids)
            print(f"[couchpi] Launched {name} (PID {proc.pid})")
        except Exception as e:
            print(f"[couchpi] Failed to launch {name}: {e}", file=sys.stderr)

    def kill(self, app):
        """SIGTERM, then SIGKILL after 2s if still alive."""
        name = app["name"]
        pid = self.pids.get(name)
        if pid is None:
            return
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(2)
            try:
                os.kill(pid, 0)  # still alive?
                os.kill(pid, signal.SIGKILL)
                print(f"[couchpi] SIGKILLed {name} (PID {pid})")
            except ProcessLookupError:
                print(f"[couchpi] {name} exited cleanly after SIGTERM")
        except ProcessLookupError:
            pass
        self.pids.pop(name, None)
        save_pids(self.pids)

# ---------------------------------------------------------------------------
# Gamepad input via evdev (optional)
# ---------------------------------------------------------------------------

GAMEPAD_KEY_MAP = {
    # d-pad as hat axis events are handled separately
    # These are button codes for a typical knockoff PS3 pad
    304: "enter",   # BTN_SOUTH / A / Cross
    308: "enter",   # BTN_NORTH
}

GAMEPAD_ABS_MAP = {
    # ABS_HAT0X: left=-1, right=+1
    # ABS_HAT0Y: up=-1, down=+1
    16: {-1: "left", 1: "right"},  # ABS_HAT0X
    17: {-1: "up",   1: "down"},   # ABS_HAT0Y
}

def start_gamepad_thread(key_callback):
    """Try to open a gamepad via evdev and forward key events."""
    try:
        import evdev
    except ImportError:
        print("[couchpi] python3-evdev not installed; gamepad input disabled", file=sys.stderr)
        return

    def _find_gamepad():
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
                caps = dev.capabilities()
                # Look for a device with buttons and absolute axes (gamepad-like)
                if evdev.ecodes.EV_KEY in caps and evdev.ecodes.EV_ABS in caps:
                    return dev
            except Exception:
                continue
        return None

    def _loop():
        import evdev
        dev = _find_gamepad()
        if dev is None:
            print("[couchpi] No gamepad found", file=sys.stderr)
            return
        print(f"[couchpi] Gamepad: {dev.name}")
        try:
            for event in dev.read_loop():
                if event.type == evdev.ecodes.EV_KEY and event.value == 1:
                    action = GAMEPAD_KEY_MAP.get(event.code)
                    if action:
                        GLib.idle_add(key_callback, action)
                elif event.type == evdev.ecodes.EV_ABS:
                    axis_map = GAMEPAD_ABS_MAP.get(event.code)
                    if axis_map and event.value != 0:
                        action = axis_map.get(event.value)
                        if action:
                            GLib.idle_add(key_callback, action)
        except Exception as e:
            print(f"[couchpi] Gamepad disconnected: {e}", file=sys.stderr)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()

# ---------------------------------------------------------------------------
# AppIcon widget
# ---------------------------------------------------------------------------

class AppIcon(Gtk.Box):
    SELECTED_SIZE = 128
    UNSELECTED_SIZE = 96

    def __init__(self, app, proc_manager):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.app = app
        self.proc_manager = proc_manager
        self._selected = False

        # Load icon in background to avoid blocking UI
        self._pixbuf_selected = None
        self._pixbuf_unselected = None
        threading.Thread(target=self._load_icons, daemon=True).start()

        self.image = Gtk.Image()
        self.image.set_size_request(self.SELECTED_SIZE, self.SELECTED_SIZE)
        self.append(self.image)

        # Running dot (hidden until a process is tracked)
        self.dot = Gtk.Label(label="●")
        self.dot.add_css_class("running-dot")
        self.dot.set_visible(False)
        self.append(self.dot)

        self.set_size_request(self.SELECTED_SIZE + 32, self.SELECTED_SIZE + 48)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)

        self.update_state(False)

    def _load_icons(self):
        path = get_icon_path(self.app)
        pb_sel = load_pixbuf(path, self.SELECTED_SIZE)
        pb_unsel = load_pixbuf(path, self.UNSELECTED_SIZE)
        if pb_sel is None:
            pb_sel = make_fallback_texture(self.SELECTED_SIZE)
        if pb_unsel is None:
            pb_unsel = make_fallback_texture(self.UNSELECTED_SIZE)
        self._pixbuf_selected = pb_sel
        self._pixbuf_unsel = pb_unsel
        GLib.idle_add(self._apply_icon)

    def _apply_icon(self):
        pb = self._pixbuf_selected if self._selected else self._pixbuf_unsel
        if pb:
            self.image.set_from_pixbuf(pb)
        return False

    def update_state(self, selected):
        self._selected = selected
        if selected:
            self.image.set_size_request(self.SELECTED_SIZE, self.SELECTED_SIZE)
            self.image.remove_css_class("icon-unselected")
            self.image.add_css_class("icon-selected")
        else:
            self.image.set_size_request(self.UNSELECTED_SIZE, self.UNSELECTED_SIZE)
            self.image.remove_css_class("icon-selected")
            self.image.add_css_class("icon-unselected")
        self._apply_icon()

    def update_running_dot(self):
        running = self.proc_manager.is_running(self.app["name"])
        self.dot.set_visible(running)

# ---------------------------------------------------------------------------
# Submenu widget
# ---------------------------------------------------------------------------

SUBMENU_ITEMS = [("Launch", "▶"), ("Kill", "✕")]

class Submenu(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.add_css_class("submenu-box")
        self._selected = 0
        self._rows = []

        for label, icon in SUBMENU_ITEMS:
            row = Gtk.Label(label=f"{icon}  {label}")
            row.set_xalign(0.5)
            self._rows.append(row)
            self.append(row)

        self._refresh()

    def _refresh(self):
        for i, row in enumerate(self._rows):
            row.remove_css_class("submenu-item")
            row.remove_css_class("submenu-item-selected")
            if i == self._selected:
                row.add_css_class("submenu-item-selected")
            else:
                row.add_css_class("submenu-item")

    def move(self, direction):
        self._selected = (self._selected + direction) % len(self._rows)
        self._refresh()

    @property
    def selected_action(self):
        return SUBMENU_ITEMS[self._selected][0]

# ---------------------------------------------------------------------------
# Main launcher window
# ---------------------------------------------------------------------------

class CouchPiApp(Gtk.Application):
    def __init__(self, proc_manager):
        super().__init__(application_id="jpg.slop.couchpi")
        self.proc_manager = proc_manager
        self.apps = load_apps()
        self._selected_idx = 0
        self._icon_widgets = []
        self._visible = True
        self.connect("activate", self._on_activate)

    def _on_activate(self, app):
        self.win = Gtk.ApplicationWindow(application=app)
        self.win.set_title("couchpi")
        self.win.set_decorated(False)

        # ---------------------------------------------------------------
        # gtk4-layer-shell setup — CRITICAL: must happen before present()
        # ---------------------------------------------------------------
        GtkLayerShell.init_for_window(self.win)
        GtkLayerShell.set_layer(self.win, GtkLayerShell.Layer.OVERLAY)

        # Anchor to all edges → fullscreen coverage
        for edge in (
            GtkLayerShell.Edge.TOP,
            GtkLayerShell.Edge.BOTTOM,
            GtkLayerShell.Edge.LEFT,
            GtkLayerShell.Edge.RIGHT,
        ):
            GtkLayerShell.set_anchor(self.win, edge, True)

        # -1 = claim the full exclusive zone (takes precedence over other surfaces)
        GtkLayerShell.set_exclusive_zone(self.win, -1)

        # Grab keyboard exclusively so arrow keys work while launcher is open
        GtkLayerShell.set_keyboard_mode(self.win, GtkLayerShell.KeyboardMode.EXCLUSIVE)
        # ---------------------------------------------------------------

        # Load CSS
        ensure_default_css()
        css_provider = Gtk.CssProvider()
        css_provider.load_from_path(STYLE_CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        self._build_ui()

        self.win.present()

        # Verify fullscreen after the window is mapped
        GLib.timeout_add(500, self._verify_fullscreen)

        # Start IPC server
        start_ipc_server(self._toggle_visibility)

        # Start gamepad thread
        start_gamepad_thread(self._on_gamepad_key)

        # Clock and running-dot refresh
        GLib.timeout_add(1000, self._update_clock)
        GLib.timeout_add(2000, self._update_running_dots)

    def _build_ui(self):
        # Root overlay: allows clock to float top-right over the main layout
        self.overlay = Gtk.Overlay()
        self.win.set_child(self.overlay)

        # --- Background / main content ---
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.main_box.set_valign(Gtk.Align.FILL)
        self.main_box.set_halign(Gtk.Align.FILL)
        self.overlay.set_child(self.main_box)

        # Spacer pushes icons to ~40% from top
        top_spacer = Gtk.Box()
        top_spacer.set_vexpand(True)
        top_spacer.set_valign(Gtk.Align.FILL)
        self.main_box.append(top_spacer)

        # Icon row
        self.icon_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=32)
        self.icon_row.set_halign(Gtk.Align.CENTER)
        self.icon_row.set_valign(Gtk.Align.CENTER)
        self.main_box.append(self.icon_row)

        # App label (selected app name)
        self.app_label = Gtk.Label(label="")
        self.app_label.add_css_class("app-label")
        self.app_label.set_halign(Gtk.Align.CENTER)
        self.main_box.append(self.app_label)

        # Submenu
        self.submenu = Submenu()
        self.submenu.set_halign(Gtk.Align.CENTER)
        self.main_box.append(self.submenu)

        # Bottom spacer
        bottom_spacer = Gtk.Box()
        bottom_spacer.set_vexpand(True)
        self.main_box.append(bottom_spacer)

        # --- Clock overlay (top-right) ---
        clock_anchor = Gtk.Box()
        clock_anchor.set_halign(Gtk.Align.END)
        clock_anchor.set_valign(Gtk.Align.START)
        self.clock_label = Gtk.Label()
        self.clock_label.add_css_class("clock")
        clock_anchor.append(self.clock_label)
        self.overlay.add_overlay(clock_anchor)

        # Populate icons
        for app in self.apps:
            icon = AppIcon(app, self.proc_manager)
            self._icon_widgets.append(icon)
            self.icon_row.append(icon)

        self._update_selection()
        self._update_clock()

        # Keyboard handling
        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        self.win.add_controller(key_ctrl)

    def _update_selection(self):
        for i, widget in enumerate(self._icon_widgets):
            widget.update_state(i == self._selected_idx)

        if self.apps:
            self.app_label.set_text(self.apps[self._selected_idx]["name"])

    def _update_clock(self):
        self.clock_label.set_text(time.strftime("%H:%M"))
        return True  # keep repeating

    def _update_running_dots(self):
        for widget in self._icon_widgets:
            widget.update_running_dot()
        return True

    def _verify_fullscreen(self):
        """Warn if the window doesn't cover the display — layer-shell may be broken."""
        display = Gdk.Display.get_default()
        monitor = display.get_monitors().get_item(0)
        if monitor is None:
            return False
        geom = monitor.get_geometry()
        alloc_w = self.win.get_width()
        alloc_h = self.win.get_height()
        if alloc_w < geom.width or alloc_h < geom.height:
            print(
                f"WARNING: Window is {alloc_w}x{alloc_h} but display is {geom.width}x{geom.height}. "
                "gtk4-layer-shell may not be working correctly. "
                "Ensure gir1.2-gtklayershell-0.1 is installed and the compositor supports wlr-layer-shell.",
                file=sys.stderr,
            )
        else:
            print(f"[couchpi] Fullscreen OK: {alloc_w}x{alloc_h}")
        return False  # one-shot

    def _toggle_visibility(self):
        if self._visible:
            self.win.set_visible(False)
            self._visible = False
        else:
            self.win.set_visible(True)
            self._visible = True
        return False

    def _on_key_pressed(self, ctrl, keyval, keycode, state):
        key = Gdk.keyval_name(keyval)
        self._handle_key(key)
        return True  # consume event

    def _on_gamepad_key(self, action):
        key_map = {
            "left": "Left",
            "right": "Right",
            "up": "Up",
            "down": "Down",
            "enter": "Return",
        }
        self._handle_key(key_map.get(action, action))

    def _handle_key(self, key):
        if key in ("Escape", "Home", "Super_L", "Super_R"):
            self._toggle_visibility()
        elif key == "Left":
            self._selected_idx = (self._selected_idx - 1) % len(self.apps)
            self._update_selection()
        elif key == "Right":
            self._selected_idx = (self._selected_idx + 1) % len(self.apps)
            self._update_selection()
        elif key == "Up":
            self.submenu.move(-1)
        elif key == "Down":
            self.submenu.move(1)
        elif key in ("Return", "KP_Enter"):
            self._execute_submenu()

    def _execute_submenu(self):
        app = self.apps[self._selected_idx]
        action = self.submenu.selected_action
        if action == "Launch":
            self.proc_manager.launch(app)
            self._toggle_visibility()
        elif action == "Kill":
            # Run kill in a thread to avoid blocking the UI during the 2s wait
            threading.Thread(
                target=self.proc_manager.kill,
                args=(app,),
                daemon=True,
            ).start()

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    # If another instance is running, send a toggle and exit
    if os.path.exists(SOCKET_PATH):
        if send_toggle():
            print("[couchpi] Toggled existing instance")
            sys.exit(0)

    proc_manager = ProcessManager()
    app = CouchPiApp(proc_manager)
    sys.exit(app.run(sys.argv))

if __name__ == "__main__":
    main()