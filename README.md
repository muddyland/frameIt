# FrameIT

FrameIT turns a Raspberry Pi and a monitor into a self-updating movie poster display. It cycles through poster images and YouTube trailers, managed from a web-based admin panel. Multiple frames can be managed from a single server, each with its own rotation, content schedule, and pinned item.

![My Frame](https://cdn.mudhut.social/media_attachments/files/113/462/298/824/058/125/original/29ae776333bc73b9.jpeg)

---

## Features

- Upload and manage movie poster images with custom banner text above and below
- Add YouTube trailers that play automatically in kiosk mode
- Multiple frames, each independently configurable (rotation, interval, pool or pinned content)
- Per-frame agent for remote Pi management: reboot, apt update/upgrade (streamed output), network config, display and browser control
- Token-based agent registration with a one-command installer
- Agent update from the server — push new agent code without touching the Pi
- Configurable default refresh interval, overridable per frame
- Separate Configure and Agent modals in the Frames admin panel
- Dark admin UI with authentication — setup on first visit, no config files needed
- Works behind a reverse proxy
- Per-frame agent credentials, CSRF-protected admin API, and hash-verified agent updates
- Automatic schema migration on startup — safe to run from multiple workers

---

## Security model

FrameIT is built for a trusted LAN. It is not hardened against a hostile
network, and the server should not be exposed to the internet without a
reverse proxy terminating TLS and, ideally, an authenticating layer in front.

What it does do:

| | |
|---|---|
| Admin session | Signed cookie, `HttpOnly`, `SameSite=Lax`, invalidated when the password changes |
| Admin API | CSRF token required on every state-changing request |
| Login | Rate-limited with lockout; 8-character minimum password |
| Agents | Per-frame credential issued at registration, compared in constant time |
| Agent updates | Downloads are verified against a server-published SHA-256 before being applied |
| Agent proxy | Only relays a fixed allowlist of agent endpoints |
| Frames | Content endpoints accept a per-frame token derived from the server key |
| Uploads | Verified by magic bytes and size-capped |
| Pi sudo | Limited to a fixed set of commands plus one fixed-arity WiFi helper |

Two things worth knowing:

- **Agent traffic is plain HTTP.** The bearer credential is protected against
  guessing but not against sniffing. Keep frames on a network you trust, and
  firewall port 5001 to the server's address.
- **Strict modes default to off** so an upgrade cannot orphan a running frame.
  Turn them on in **Settings → Security** once your devices have caught up —
  see [UPGRADING.md](UPGRADING.md).

---

## Architecture

```
┌─────────────────────────┐        ┌──────────────────────────────┐
│   FrameIT Server        │        │   Raspberry Pi               │
│   (Flask + SQLite)      │◄──────►│   frameit-agent (port 5001)  │
│   Admin UI              │        │   Chromium kiosk (port 5000) │
└─────────────────────────┘        └──────────────────────────────┘
```

The server can run anywhere — a spare Pi, a home server, or a VPS. Each display Pi runs a lightweight agent that registers with the server using a one-time token, then receives proxied management commands through the admin UI.

---

## Hardware

### Screen
A 15" portable monitor in a custom wood frame. Portable monitors are available on Amazon at reasonable prices. 180° HDMI and USB-C adapters keep the cables tidy inside the frame. The screen is powered via a 12V-to-5V step-down supply — useful for long wall runs where voltage drop on USB power is a concern.

### Raspberry Pi
A Raspberry Pi 3B+ with a 64GB microSD card, also powered by a 12V-to-5V step-down. A Pi 4 or Pi Zero 2W will work as well.

---

## Server Installation

The server runs on any machine with Python 3.9+. It does not need to be a Raspberry Pi.

```bash
git clone https://github.com/muddyland/frameIt.git
cd frameIt
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
flask init-db
gunicorn -k gthread -w 2 --threads 8 --timeout 600 -b 0.0.0.0:5000 main:app
```

On first visit to `/admin` you will be prompted to create an admin account. No config file needed.

Threaded workers matter: a proxied `apt upgrade` streams output for minutes and
would otherwise hold a synchronous worker, stalling every frame until it
finished.

**Upgrading an existing install?** See [UPGRADING.md](UPGRADING.md).

### Running as a systemd service

```ini
[Unit]
Description=FrameIT Server
After=network.target

[Service]
WorkingDirectory=/opt/frameit
EnvironmentFile=/etc/frameit.env
ExecStart=/opt/frameit/.venv/bin/gunicorn -k gthread -w 2 --threads 8 --timeout 600 -b 0.0.0.0:5000 main:app
Restart=always

[Install]
WantedBy=multi-user.target
```

`/etc/frameit.env`:
```
DATA_DIR=/var/lib/frameit
IMAGES_DIR=/var/lib/frameit/images
VIDEOS_DIR=/var/lib/frameit/videos

# Reverse-proxy hops to trust. Set to 0 when gunicorn is exposed directly,
# otherwise a client can forge X-Forwarded-For and pick its own source address.
TRUSTED_PROXY_HOPS=1

# Set when serving over HTTPS — adds Secure to the session cookie and HSTS.
# FORCE_HTTPS=1
```

Full list of environment variables: [UPGRADING.md](UPGRADING.md#new-environment-variables).

### Reverse proxy (nginx)

```nginx
server {
    listen 80;
    server_name frameit.local;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

---

## Pi / Client Installation

FrameIT includes a one-command installer for Raspberry Pi OS Lite (64-bit recommended). It installs all dependencies, configures autologin, sets up Chromium in kiosk mode, and registers the agent with your server.

**Before running the installer:**
1. Flash a Pi with **Raspberry Pi OS Lite** using the Raspberry Pi Imager. Enable SSH and configure your user in the imager.
2. Boot and SSH in.
3. In the FrameIT admin panel, go to **Frames** and click **Generate Token**.

**Run the installer on the Pi:**
```bash
curl -fsSL http://your-server:5000/install.sh -o frameit-install.sh
sudo bash frameit-install.sh \
  --server http://your-server:5000 \
  --token <your-token>
```

The script is downloaded and run as two steps rather than piped into `sudo
bash`, so you can read it first and keep a copy of what actually ran. Serve the
server over HTTPS if it is reachable beyond a trusted LAN — the installer and
the agent's self-update both fetch code over this connection.

If your Pi user is not `pi`, pass `--user <username>`:
```bash
sudo bash frameit-install.sh \
  --server http://your-server:5000 \
  --token <your-token> \
  --user myuser
```

The installer will:
- Install `chromium-browser`, `xorg`, `openbox`, `unclutter`, `network-manager`, and Python
- Configure console autologin and start X automatically on tty1
- Launch Chromium in kiosk mode pointing at your FrameIT server
- Install and start the `frameit-agent` systemd service

**Reboot to start the kiosk:**
```bash
sudo reboot
```

### Display orientation

Rotation is configured per-frame from the admin panel (0°, 90°, 180°, 270°). Portrait mode is applied in software — no need to change anything on the Pi itself.

---

## Admin Panel

| Section  | Description |
|----------|-------------|
| Dashboard | Live status of all registered frames — frame online/offline dot, agent heartbeat indicator |
| Posters  | Upload images, set banner text, manage the rotation pool |
| Trailers | Add YouTube trailers by URL or video ID |
| Frames   | Register new frames (generate token → copy install command), set a default refresh interval, configure each display |

Each registered frame has two buttons:

- **Configure** — display name, rotation (0°/90°/180°/270°), refresh interval, content mode (pool or pinned item)
- **Agent** (enabled once the agent is installed) — system info, services, Chromium controls, display on/off, `apt update`/`apt upgrade` with live streamed output, reboot, agent self-update, network / WiFi config

---

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r tests/requirements.txt
pip install psutil   # for agent tests
pytest tests/ agent/tests/
```

Tests are isolated per test against a throwaway SQLite database. Pylint is
configured via `.pylintrc`; CI runs it as a gate.

`requirements.txt` bounds each direct dependency to a major version;
`requirements.lock` pins the exact transitive set used for container builds.
`yt-dlp` is deliberately unpinned because YouTube changes break older releases —
a scheduled CI job exercises it so a breaking release is caught here rather than
by a frame.

Run `python main.py` for a local server; it binds to `127.0.0.1` and starts
without the debugger unless you set `FLASK_DEBUG=1` and `FLASK_RUN_HOST`.

---

## License

MIT. Do what you want with it.

---

## A note on AI

This project was built with help from Claude (Anthropic's AI assistant). Writing software solo, with limited time, is hard. AI assistance made it possible to move faster, think through architecture decisions, catch bugs early, and build things that would otherwise have sat on the backlog indefinitely.

I think that's a good thing. AI used responsibly — as a collaborator, not a replacement for judgement — is genuinely useful for people building real things in the real world. Side projects, small teams, busy lives: AI helps close that gap between what you can imagine and what you can actually ship.

If you use this project, feel free to do the same.
