# LINE on Bottles

*[中文說明](README.md)*

Run the Windows LINE client on Ubuntu, inside Flatpak Bottles + Wine, isolated from your main desktop by a dedicated nested X server (Xephyr on `:99`).

This repo holds the **surrounding infrastructure** — launch flow, systemd units, input-method bridging, version switching and an installer. LINE itself and your chat history live in the Wine prefix and are not version controlled.

## Why the complexity

Running LINE directly under Bottles brings a few problems: erratic window management, stray black windows, and no CJK input. Confining it to its own Xephyr with a dedicated openbox keeps all of that inside one window instead of leaking into your desktop.

The price is two layers of isolation, which create three problems that must be solved explicitly:

| Problem | Solution |
|---|---|
| The Flatpak sandbox cannot see the host's `/tmp/.X11-unix/X99` | `socat` bridges it to TCP `127.0.0.1:6099` |
| fcitx5 only serves the main display; `:99` has no input method at all | A second, fully isolated fcitx5 instance for `:99` |
| LINE refuses to load Wine's unsigned system DLLs | Sign `system32`/`syswow64` with a locally generated certificate |

That last one is the least documented and the most likely to stop you cold: without it LINE simply will not start.

## Quick start

```bash
git clone https://github.com/sgeric910601/line-bottles ~/line-bottles
cd ~/line-bottles
./install.sh --dry-run      # see what it would do
./install.sh
```

If a bottle already exists the installer skips creating one and only installs the shell layer. To build from scratch, place LINE's official installer at `~/Downloads/LineInst.exe` (or set `LINE_INSTALLER=`).

## Commands

```bash
line-bottles                  # start (same as the desktop entry)
line-bottles --stop           # stop LINE and any leftover black windows
line-bottles --recover        # force-restart the whole environment
line-bottles --status         # JSON status

line-version status           # current version, layout, auto-update state
line-version list             # installed versions
line-version use 26.4.2.3957  # switch
line-version pin              # pin a version (disables LINE's auto-updater)
```

See [docs/versions.md](docs/versions.md) for details (documentation is in Traditional Chinese).

## Architecture

```
GNOME desktop (:1)
└── Xephyr :99                    a 1280x800 nested X server
    ├── openbox                   window management
    ├── fcitx5 (own instance)     CJK input, private dbus + isolated config
    └── Wine / LINE.exe           connects over the socat TCP bridge
```

Five systemd user units are orchestrated by `line-session.service`. Startup and shutdown order are **not** simply the reverse of each other — the reasoning sits next to `START_ORDER` / `STOP_ORDER` in `lib/session.py` and in [docs/architecture.md](docs/architecture.md).

Everything under `lib/` uses only the Python standard library — no third-party dependencies.

## Known limitations

- **No drag and drop.** Xephyr is a separate X server, and X11 drag-and-drop (XDND) relies on client messages within a single X server. Neither direction works. The only real fix would be dropping Xephyr, which brings back the window-management problems it was introduced to solve.
- **Rendering is slow.** `LIBGL_ALWAYS_SOFTWARE=true` routes all GL through llvmpipe (CPU software rendering). That is the current ceiling on smoothness.
- **The file picker only sees `~/Downloads` and `~/Pictures`.** The Flatpak sandbox deliberately exposes nothing else; copy files there before sending them.

## Troubleshooting

Start with [docs/troubleshooting.md](docs/troubleshooting.md). Besides a symptom table, it records **approaches that were tried and disproved** — hypotheses that looked right but were refuted by the logs. Re-treading them only wastes time.

The single most useful lesson is there too: `wine-debug.log` ending in `X connection broken` looks exactly like an X server crash, but `journalctl` showed it was an ordinary SIGTERM shutdown. Wine's logs alone will send you in the wrong direction; always cross-check with the journal.

## Layout

| Path | Contents |
|---|---|
| `bin/*.in` | Launcher templates; `install.sh` substitutes real paths |
| `lib/` | `session.py` (orchestration), `x11.py` (ctypes Xlib), `version.py`, `peversion.py` |
| `systemd/` | Five user units |
| `bottle/` | Bottle creation, DLL signing, certificate config |
| `docs/` | Architecture, troubleshooting, version management |

## A note on the signing certificate

`bottle/sign-dlls.sh` generates a self-signed certificate and uses it to sign the Wine system DLLs inside the prefix. The certificate:

- is generated locally at install time and never committed
- is only trusted inside that Wine prefix; it never touches the host trust store
- identifies itself honestly as `LINE-Bottles Local Wine Signing`, rather than impersonating a vendor

This exists purely so LINE's own DLL signature check passes. It is a Wine interoperability shim, nothing more.

## License

MIT — see [LICENSE](LICENSE). This project packages infrastructure only; it does not redistribute LINE, Wine, Bottles or DXVK.
