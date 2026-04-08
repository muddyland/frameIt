# FrameIT

FrameIT turns a Raspberry Pi and a monitor into a self-updating movie poster display. It cycles through poster images and YouTube trailers, managed from a web-based admin panel. Multiple frames can be managed from a single server, each with its own rotation, content schedule, and pinned item.

![My Frame](https://cdn.mudhut.social/media_attachments/files/113/462/298/824/058/125/original/29ae776333bc73b9.jpeg)

---

## Features

- Upload and manage movie poster images with custom banner text above and below
- Add YouTube trailers that play automatically in kiosk mode
- Multiple frames, each independently configurable (rotation, interval, pool or pinned content)
- Per-frame agent for remote Pi management: reboot, apt update/upgrade, network config, display and browser control
- Token-based agent registration with a one-command installer
- Dark admin UI with authentication — setup on first visit, no config files needed
- Works behind a reverse proxy

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
gunicorn -w 1 -b 0.0.0.0:5000 main:app
```

On first visit to `/admin` you will be prompted to create an admin account. No config file needed.

### Running as a systemd service

```ini
[Unit]
Description=FrameIT Server
After=network.target

[Service]
WorkingDirectory=/opt/frameit
EnvironmentFile=/etc/frameit.env
ExecStart=/opt/frameit/.venv/bin/gunicorn -w 1 -b 0.0.0.0:5000 main:app
Restart=always

[Install]
WantedBy=multi-user.target
```

`/etc/frameit.env`:
```
DATA_DIR=/var/lib/frameit
IMAGES_DIR=/var/lib/frameit/images
```

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
curl -sSL http://your-server:5000/install.sh | sudo bash -s -- \
  --server http://your-server:5000 \
  --token <your-token>
```

If your Pi user is not `pi`, pass `--user <username>`:
```bash
curl -sSL http://your-server:5000/install.sh | sudo bash -s -- \
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
| Dashboard | Live status of all registered frames |
| Posters  | Upload images, set banner text, manage the rotation pool |
| Trailers | Add YouTube trailers by URL or video ID |
| Frames   | Register new frames (generate token → copy install command), configure each display, and manage the agent |

---

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install psutil   # for agent tests
pytest
```

Tests use an in-memory SQLite database and are isolated per test. Pylint is configured via `.pylintrc`.

---

## License

MIT. Do what you want with it.

---

## A note on AI

This project was built with help from Claude (Anthropic's AI assistant). Writing software solo, with limited time, is hard. AI assistance made it possible to move faster, think through architecture decisions, catch bugs early, and build things that would otherwise have sat on the backlog indefinitely.

I think that's a good thing. AI used responsibly — as a collaborator, not a replacement for judgement — is genuinely useful for people building real things in the real world. Side projects, small teams, busy lives: AI helps close that gap between what you can imagine and what you can actually ship.

If you use this project, feel free to do the same.
