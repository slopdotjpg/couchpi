# couchpi

A PSP/XMB-style fullscreen TV launcher for Raspberry Pi 5, running on Raspberry Pi OS Lite with Labwc (Wayland). Navigate your apps with a remote keyboard or knockoff gamepad, launch and kill them without leaving the couch.

---

## Full stack overview

```
Raspberry Pi OS Bookworm Lite (headless base)
  └── Labwc (Wayland compositor, wlroots-based)
        ├── couchpi launcher (GTK4 + gtk4-layer-shell, overlay layer)
        ├── RetroArch (gaming frontend)
        ├── Jellyfin Media Player (media)
        └── Chromium (kiosk mode for web apps)
```

**Why this stack?**

| Component | Why |
|-----------|-----|
| RPi OS Lite | No desktop bloat; starts faster and leaves more RAM for emulation |
| Labwc | Lightweight wlroots Wayland compositor; supports layer-shell protocol |
| gtk4-layer-shell | Lets a GTK4 window claim a Wayland overlay layer, so it sits *above* all other apps without killing them |
| Python + GTK4 | Single-file, readable, easy to hack |

---

## Hardware

- Raspberry Pi 5 (4 GB or 8 GB)
- Any HDMI TV or monitor
- Cheap IR remote keyboard (arrow keys + Enter)
- Optional: knockoff PS3 / generic gamepad (D-pad + face buttons)

---

## Installation

### 1. Flash and boot

Flash Raspberry Pi OS Bookworm **Lite** (64-bit) with the Raspberry Pi Imager. Enable SSH in the imager's settings if you want headless setup.

### 2. Install a Wayland session

```bash
sudo apt install labwc xwayland
```

Configure Labwc to start on login (or use `~/.bash_profile`):

```bash
# ~/.bash_profile
if [ -z "$WAYLAND_DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
    exec labwc
fi
```

### 3. Install couchpi

```bash
git clone https://github.com/slopdotjpg/couchpi.git
cd couchpi
bash setup.sh
```

`setup.sh` will:
- Install all apt and pip dependencies
- Create `~/.config/tv-launcher/apps.json` from the example
- Append the launcher to `~/.config/labwc/autostart`
- Add the Super/Home key binding to `~/.config/labwc/rc.xml`

### 4. Configure your apps

Edit `~/.config/tv-launcher/apps.json`:

```json
[
  {
    "name": "RetroArch",
    "launch": "retroarch",
    "icon": "~/.config/tv-launcher/icons/retroarch.svg"
  },
  {
    "name": "Jellyfin",
    "launch": "jellyfinmediaplayer",
    "icon": null
  },
  {
    "name": "Home Assistant",
    "url": "http://homeassistant.local:8123",
    "launch": "chromium-browser --kiosk http://homeassistant.local:8123",
    "icon": null
  }
]
```

Apps with a `url` and no `icon` will have their favicon fetched automatically on first launch and cached in `~/.config/tv-launcher/icons/`.

### 5. Reboot or restart Labwc

```bash
labwc --reconfigure   # or just reboot
```

---

## Usage

| Key / Button | Action |
|---|---|
| ← / → (arrow or D-pad) | Navigate between apps |
| ↓ | Open the submenu for the selected app |
| ↑ / ↓ | Navigate submenu (Launch / Kill) |
| Enter / A button | Confirm selection |
| Escape / Home / Super | Hide the launcher |

A green dot on an app icon means a tracked process for that app is currently running.

---

## How it works

### Layer shell overlay

`gtk4-layer-shell` uses the `wlr-layer-shell` Wayland protocol to create a surface on the **overlay** layer. This is above normal windows and even above fullscreen apps, so the launcher appears on top of RetroArch or Jellyfin without needing to kill them first.

### IPC / toggle

When you press Home, Labwc executes `python3 launcher.py`. The script checks for a running instance via a Unix socket (`/tmp/couchpi.sock`). If found, it sends a `toggle` command and exits — so the existing process shows or hides itself. If not found, it becomes the server and starts fresh.

### Process management

Apps are launched as detached subprocesses (new session group) so they survive if the launcher crashes or restarts. The launcher tracks PIDs in memory during its session and uses `os.kill(pid, 0)` to check liveness.

### Focus / bring-to-front (Wayland limitation)

Wayland intentionally prevents apps from stealing focus. When you "Launch" an already-running app, couchpi attempts an XDG Activation request via `gdbus`, but the compositor is not required to honour it. On Labwc this works for well-behaved Wayland clients. XWayland clients may not respond — this is a known protocol limitation, not a bug.

### Gamepad

The gamepad thread reads raw joystick events from `/dev/input/js0` using Python's `struct` module — no extra libraries needed. If no gamepad is connected the thread exits silently.

---

## Labwc configuration reference

`~/.config/labwc/autostart` — start the launcher when Labwc starts:
```
python3 /path/to/couchpi/launcher.py &
```

`~/.config/labwc/rc.xml` — bind Super/Home key:
```xml
<keybind key="Super_L">
  <action name="Execute">
    <command>python3 /path/to/couchpi/launcher.py</command>
  </action>
</keybind>
```

---

## Dependencies

| Package | Install |
|---|---|
| python3-gi | `sudo apt install python3-gi` |
| gir1.2-gtk-4.0 | `sudo apt install gir1.2-gtk-4.0` |
| gtk4-layer-shell | `sudo apt install gtk4-layer-shell gir1.2-gtk4-layer-shell` |
| python3-requests | `sudo apt install python3-requests` |
| fonts-noto-color-emoji | `sudo apt install fonts-noto-color-emoji` |

All dependencies are available in Raspberry Pi OS Bookworm's default apt repositories.

---

## License

MIT — see [LICENSE](LICENSE).
