#!/usr/bin/env python3
"""
FrameIT Agent — runs on the Raspberry Pi as a system-level service.
Registers with the FrameIT server using a one-time token, then accepts
proxied commands from the server (system info, reboot, apt, network, display).

Environment variables:
    FRAMEIT_SERVER  - Base URL of the FrameIT server (e.g. http://192.168.1.10:5000)
    FRAMEIT_TOKEN   - One-time registration token generated in the admin UI
    AGENT_PORT      - Port this agent listens on (default: 5001)
"""

import hashlib
import os
import pwd
import re
import socket
import subprocess
import sys
import threading
import time
from functools import wraps

import psutil
import requests
from flask import Flask, Response, jsonify, request

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FRAMEIT_SERVER = os.environ.get('FRAMEIT_SERVER', '').rstrip('/')
FRAMEIT_TOKEN = os.environ.get('FRAMEIT_TOKEN', '')
AGENT_PORT = int(os.environ.get('AGENT_PORT', 5001))
KIOSK_USER = os.environ.get('KIOSK_USER', 'pi')

app = Flask(__name__)

# Stored after first successful registration
_frame_id = None

# Hash of this agent.py file — used to detect when an update is available
def _compute_version():
    try:
        with open(os.path.abspath(__file__), 'rb') as _f:
            return hashlib.sha256(_f.read()).hexdigest()[:12]
    except Exception:
        return 'unknown'

AGENT_VERSION = _compute_version()

# ---------------------------------------------------------------------------
# Input sanitization
# ---------------------------------------------------------------------------

def _sanitize(value, max_len=255):
    """Strip null bytes and non-printable characters; enforce max length."""
    if not isinstance(value, str):
        return ''
    return ''.join(ch for ch in value if ch.isprintable())[:max_len]

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def require_token(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        if not auth.startswith('Bearer ') or auth[7:] != FRAMEIT_TOKEN:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return wrapper

# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------

@app.route('/health')
@require_token
def health():
    return jsonify({
        'ok': True,
        'hostname': socket.gethostname(),
        'uptime_seconds': int(time.time() - psutil.boot_time()),
        'version': AGENT_VERSION,
    })


@app.route('/system/info')
@require_token
def system_info():
    cpu_temp = None
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            for key in ('cpu_thermal', 'coretemp', 'cpu-thermal'):
                if key in temps:
                    cpu_temp = round(temps[key][0].current, 1)
                    break
    except Exception:
        pass

    return jsonify({
        'cpu_percent': psutil.cpu_percent(interval=0.5),
        'ram_percent': psutil.virtual_memory().percent,
        'disk_percent': psutil.disk_usage('/').percent,
        'cpu_temp': cpu_temp,
        'hostname': socket.gethostname(),
        'uptime_seconds': int(time.time() - psutil.boot_time()),
    })


@app.route('/system/reboot', methods=['POST'])
@require_token
def reboot():
    def _reboot():
        time.sleep(2)
        subprocess.run(['sudo', 'reboot'], check=False)
    threading.Thread(target=_reboot, daemon=True).start()
    return jsonify({'message': 'Rebooting in 2 seconds'})


@app.route('/system/agent-update', methods=['POST'])
@require_token
def agent_update():
    def _do_update():
        time.sleep(2)
        agent_path = os.path.abspath(__file__)
        agent_dir  = os.path.dirname(agent_path)
        req_path   = os.path.join(agent_dir, 'requirements.txt')
        pip        = os.path.join(os.path.dirname(sys.executable), 'pip')
        try:
            r = requests.get(f'{FRAMEIT_SERVER}/agent.py', timeout=30)
            r.raise_for_status()
            with open(agent_path, 'w', encoding='utf-8') as f:
                f.write(r.text)
            r = requests.get(f'{FRAMEIT_SERVER}/agent-requirements.txt', timeout=30)
            r.raise_for_status()
            with open(req_path, 'w', encoding='utf-8') as f:
                f.write(r.text)
            subprocess.run([pip, 'install', '--quiet', '-r', req_path], check=False)
        except Exception as e:
            print(f'[agent] Update download failed: {e}')
            return
        subprocess.run(['sudo', 'systemctl', 'restart', 'frameit-agent'], check=False)
    threading.Thread(target=_do_update, daemon=True).start()
    return jsonify({'message': 'Update started — agent will restart in a few seconds'})


_STREAM_HEADERS = {'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}


@app.route('/system/update', methods=['POST'])
@require_token
def apt_update():
    try:
        proc = subprocess.Popen(
            ['sudo', 'apt-get', 'update'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except OSError as e:
        return jsonify({'error': str(e)}), 500

    def _stream():
        yield from proc.stdout
        proc.wait()

    return Response(_stream(), mimetype='text/plain', headers=_STREAM_HEADERS)


@app.route('/system/upgrade', methods=['POST'])
@require_token
def apt_upgrade():
    try:
        proc = subprocess.Popen(
            ['sudo', 'apt-get', 'upgrade', '-y'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            env={**os.environ, 'DEBIAN_FRONTEND': 'noninteractive'},
        )
    except OSError as e:
        return jsonify({'error': str(e)}), 500

    def _stream():
        yield from proc.stdout
        proc.wait()

    return Response(_stream(), mimetype='text/plain', headers=_STREAM_HEADERS)


@app.route('/system/services')
@require_token
def service_status():
    services = ['frameit-agent', 'frameit-ui']
    result = {}
    for svc in services:
        r = subprocess.run(['systemctl', 'is-active', svc], capture_output=True, text=True)
        result[svc] = r.returncode == 0
    return jsonify(result)


@app.route('/system/services/<name>/restart', methods=['POST'])
@require_token
def restart_service(name):
    allowed = {'frameit-agent', 'frameit-ui'}
    if name not in allowed:
        return jsonify({'error': 'Unknown service'}), 400
    r = subprocess.run(['sudo', 'systemctl', 'restart', name], capture_output=True, text=True)
    return jsonify({'ok': r.returncode == 0, 'output': r.stderr})

# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

@app.route('/network/status')
@require_token
def network_status():
    interfaces = []
    for iface, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            if addr.family == socket.AF_INET and not iface.startswith('lo'):
                interfaces.append({'name': iface, 'ip': addr.address})

    ssid = None
    try:
        r = subprocess.run(['iwgetid', '-r'], capture_output=True, text=True)
        ssid = r.stdout.strip() or None
    except FileNotFoundError:
        try:
            r = subprocess.run(['nmcli', '-t', '-f', 'active,ssid', 'dev', 'wifi'],
                               capture_output=True, text=True)
            for line in r.stdout.splitlines():
                if line.startswith('yes:'):
                    ssid = line.split(':', 1)[1]
                    break
        except FileNotFoundError:
            pass

    return jsonify({'hostname': socket.gethostname(), 'interfaces': interfaces, 'ssid': ssid})


@app.route('/network/hostname', methods=['POST'])
@require_token
def set_hostname():
    body = request.get_json(silent=True) or {}
    hostname = _sanitize(body.get('hostname', ''))
    if not hostname or not re.match(r'^[a-zA-Z0-9\-]{1,63}$', hostname):
        return jsonify({'error': 'Invalid hostname'}), 400
    r = subprocess.run(['sudo', 'hostnamectl', 'set-hostname', hostname], capture_output=True, text=True)
    if r.returncode != 0:
        return jsonify({'error': r.stderr}), 500
    return jsonify({'ok': True, 'hostname': hostname})


@app.route('/network/wifi/scan')
@require_token
def wifi_scan():
    try:
        r = subprocess.run(
            ['nmcli', '-t', '-f', 'ssid,signal', 'dev', 'wifi', 'list'],
            capture_output=True, text=True, timeout=15
        )
        networks = []
        for line in r.stdout.splitlines():
            parts = line.split(':')
            if parts[0]:
                networks.append({'ssid': parts[0], 'signal': parts[1] if len(parts) > 1 else ''})
        return jsonify({'networks': networks})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/network/wifi/connect', methods=['POST'])
@require_token
def wifi_connect():
    body = request.get_json(silent=True) or {}
    ssid     = _sanitize(body.get('ssid', ''),     max_len=32)   # 802.11 max SSID length
    password = _sanitize(body.get('password', ''), max_len=63)   # WPA2-PSK max length
    if not ssid:
        return jsonify({'error': 'ssid is required'}), 400
    if not ssid.isprintable():
        return jsonify({'error': 'Invalid SSID'}), 400
    cmd = ['sudo', 'nmcli', 'dev', 'wifi', 'connect', ssid]
    if password:
        cmd += ['password', password]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return jsonify({'error': r.stderr}), 500
    return jsonify({'ok': True, 'output': r.stdout})

# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def _xenv():
    """Environment vars needed to issue X11 commands as the kiosk user."""
    try:
        xauth = os.path.join(pwd.getpwnam(KIOSK_USER).pw_dir, '.Xauthority')
    except KeyError:
        xauth = f'/home/{KIOSK_USER}/.Xauthority'
    return {'DISPLAY': ':0', 'XAUTHORITY': xauth}


def _display_is_on():
    r = subprocess.run(['xset', 'q'], capture_output=True, text=True, env=_xenv())
    return 'DPMS is Disabled' in r.stdout or 'Monitor is On' in r.stdout


@app.route('/display')
@require_token
def display_status():
    try:
        return jsonify({'on': _display_is_on()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/display/on', methods=['POST'])
@require_token
def display_on():
    env = _xenv()
    subprocess.run(['xset', 'dpms', 'force', 'on'], check=False, env=env)
    subprocess.run(['xset', '-dpms'], check=False, env=env)
    return jsonify({'ok': True})


@app.route('/display/off', methods=['POST'])
@require_token
def display_off():
    env = _xenv()
    subprocess.run(['xset', '+dpms'], check=False, env=env)
    subprocess.run(['xset', 'dpms', 'force', 'off'], check=False, env=env)
    return jsonify({'ok': True})

# ---------------------------------------------------------------------------
# Browser (Chromium kiosk) — managed via frameit-ui systemd service
# ---------------------------------------------------------------------------

@app.route('/browser/status')
@require_token
def browser_status():
    r = subprocess.run(['systemctl', 'is-active', 'frameit-ui'], capture_output=True, text=True)
    return jsonify({'running': r.returncode == 0})


@app.route('/browser/start', methods=['POST'])
@require_token
def browser_start():
    r = subprocess.run(['sudo', 'systemctl', 'start', 'frameit-ui'], capture_output=True, text=True)
    return jsonify({'ok': r.returncode == 0})


@app.route('/browser/stop', methods=['POST'])
@require_token
def browser_stop():
    r = subprocess.run(['sudo', 'systemctl', 'stop', 'frameit-ui'], capture_output=True, text=True)
    return jsonify({'ok': r.returncode == 0})


@app.route('/browser/restart', methods=['POST'])
@require_token
def browser_restart():
    r = subprocess.run(['sudo', 'systemctl', 'restart', 'frameit-ui'], capture_output=True, text=True)
    return jsonify({'ok': r.returncode == 0})


# ---------------------------------------------------------------------------
# Registration + heartbeat
# ---------------------------------------------------------------------------

def register():
    """POSTs to the FrameIT server to register this agent. Retries indefinitely."""
    global _frame_id
    while True:
        try:
            resp = requests.post(
                f'{FRAMEIT_SERVER}/api/agents/register',
                json={'token': FRAMEIT_TOKEN, 'hostname': socket.gethostname(), 'port': AGENT_PORT},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                _frame_id = data.get('frame_id')
                print(f'[agent] Registered as frame #{_frame_id}')
                return
            print(f'[agent] Registration failed ({resp.status_code}): {resp.text}')
        except Exception as e:
            print(f'[agent] Registration error: {e}')
        time.sleep(15)


def heartbeat_loop():
    """Sends a heartbeat to the FrameIT server every 60 seconds."""
    while True:
        time.sleep(60)
        if not _frame_id:
            continue
        try:
            requests.post(
                f'{FRAMEIT_SERVER}/api/agents/{_frame_id}/heartbeat',
                json={'version': AGENT_VERSION},
                timeout=5,
            )
        except Exception:
            pass


if __name__ == '__main__':
    if not FRAMEIT_SERVER or not FRAMEIT_TOKEN:
        print('[agent] FRAMEIT_SERVER and FRAMEIT_TOKEN must be set.')
        raise SystemExit(1)

    threading.Thread(target=register, daemon=True).start()
    threading.Thread(target=heartbeat_loop, daemon=True).start()

    app.run(host='0.0.0.0', port=AGENT_PORT)
