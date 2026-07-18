# couchpi

A fullscreen TV launcher for Raspberry Pi 5, styled after the PS3 XMB (CrossMediaBar) interface.

## Stack

```
Raspberry Pi OS Bookworm (Lite, 64-bit)
  └── Labwc (Wayland compositor, wlr-protocols)
        └── couchpi (GTK4 + gtk4-layer-shell, wlr-layer-shell overlay)
              ├── App icons (SVG/PNG/ICO, auto-fetched for web apps)
              ├── Process management (detached subprocesses, PID tracking)
              ├── IPC (Unix socket for Home key toggle)
              └── Gamepad input (evdev, d-pad + A button)
```

### Why each layer

**Raspberry Pi OS Lite** — minimal Debian base without a desktop environment. Gives full control over what runs; nothing wasted on a GUI stack we don't use.

**Labwc** — a wlr-based Wayland compositor that implements the `wlr-layer-shell` protocol. This is the protocol that allows couchpi to claim a fullscreen OVERLAY layer on top of all other windows. X11-based compositors don't support this. GNOME/KDE do, but they're too heavy and add their own shell on top.

**GTK4 + gtk4-layer-shell** — GTK4 is the modern toolkit with Wayland-native rendering. `gtk4-layer-shell` is a small library that wires a GTK4 window to `wlr-layer-shell`, letting it become a proper layer surface rather than a regular application window. Without this, the window would be managed by the compositor just like any other app.

## Installation

```bash
git clone https://github.com/slopdotjpg/couchpi
cd couchpi
chmod +x setup.sh
./setup.sh
```

`setup.sh` will:
- Install apt dependencies (`python3-gi`, `gir1.2-gtklayershell-0.1`, `python3-evdev`, etc.)
- Copy `launcher.py` to `~/.config/couchpi/`
- Write a default `~/.config/couchpi/apps.json`
- Add couchpi to Labwc's `autostart`
- Add a `Home` key binding to Labwc's `rc.xml`

Reboot or restart Labwc for changes to take effect.

## Configuration

Edit `~/.config/couchpi/apps.json`:

```json
[
  {
    "name": "RetroArch",
    "launch": "retroarch",
    "icon": "~/.config/couchpi/icons/retroarch.svg"
  },
  {
    "name": "My Web App",
    "url": "http://localhost:3000",
    "launch": "chromium --kiosk http://localhost:3000",
    "icon": null
  }
]
```

When `icon` is `null` and `url` is set, couchpi fetches the favicon automatically on first launch and caches it in `~/.config/couchpi/icons/`.

### Theming

Edit `~/.config/couchpi/style.css`. The file is created with defaults on first run. Changes take effect on next launcher start.

## Controls

| Input | Action |
|-------|--------|
| ← → arrow keys | Navigate apps |
| ↑ ↓ arrow keys | Navigate submenu (Launch / Kill) |
| Enter | Confirm action |
| Escape / Home | Toggle launcher visibility |
| Gamepad d-pad | Same as arrow keys |
| Gamepad A / Cross | Same as Enter |

## Files

```
~/.config/couchpi/
  launcher.py     — main launcher (copied here by setup.sh)
  toggle.py       — IPC toggle script (invoked by Home key binding)
  apps.json       — app definitions
  style.css       — GTK4 CSS theme
  pids.json       — tracked PIDs (survives restarts)
  icons/          — cached favicons and custom icons
  couchpi.sock    — Unix socket (created at runtime)
```

## Dependencies

```
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-gtklayershell-0.1 python3-evdev
```

## License

MIT