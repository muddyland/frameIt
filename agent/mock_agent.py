#!/usr/bin/env python3
"""
FrameIT Mock Agent — for local development and testing.

Mimics the real agent API with realistic fake data and stateful toggles,
so you can exercise all admin UI controls without a Raspberry Pi.

Usage:
    1. Start the FrameIT server locally.
    2. Go to Admin → Frames, generate a registration token.
    3. Run:
           FRAMEIT_SERVER=http://localhost:5000 \\
           FRAMEIT_TOKEN=<token> \\
           python agent/mock_agent.py
    4. The mock registers itself, appears in the Frames list, and responds
       to all agent controls.
"""

import os
import random
import socket
import threading
import time
from functools import wraps

import requests
from flask import Flask, jsonify, request

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FRAMEIT_SERVER = os.environ.get('FRAMEIT_SERVER', 'http://localhost:5000').rstrip('/')
FRAMEIT_TOKEN  = os.environ.get('FRAMEIT_TOKEN', '')
AGENT_PORT     = int(os.environ.get('AGENT_PORT', 5001))

# Deliberately stale version so the update alert is always visible in mock mode.
# Mutated to the real server version when agent-update is triggered.
_mock_version = 'deadbeef0000'

app = Flask(__name__)
_frame_id = None
_restarting = False  # True while simulating a restart/update

# ---------------------------------------------------------------------------
# Stateful mock values
# ---------------------------------------------------------------------------

_state = {
    'display_on': True,
    'hostname': os.environ.get('MOCK_HOSTNAME', 'frameit-mock'),
    'wifi_ssid': 'MockNetwork',
}

_MOCK_SERVICES = {
    'frameit-agent': True,
    'frameit-ui':    True,
}

_MOCK_NETWORKS = [
    {'ssid': 'MockNetwork',   'signal': '85'},
    {'ssid': 'Neighbors_2.4', 'signal': '60'},
    {'ssid': 'CoffeeShopWifi','signal': '40'},
]

_APT_UPDATE_OUTPUT = """\
Hit:1 http://deb.debian.org/debian bookworm InRelease
Hit:2 http://deb.debian.org/debian-security bookworm-security InRelease
Hit:3 http://deb.debian.org/debian bookworm-updates InRelease
Hit:4 http://archive.raspberrypi.com/debian bookworm InRelease
Reading package lists... Done
"""

_APT_UPGRADE_OUTPUT = """\
Reading package lists... Done
Building dependency tree... Done
Reading state information... Done
Calculating upgrade... Done
The following packages will be upgraded:
  libraspberrypi-bin libraspberrypi-dev raspberrypi-bootloader
3 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.
Need to get 18.4 MB of archives.
After this operation, 0 B of additional disk space will be used.
Get:1 http://archive.raspberrypi.com/debian bookworm/main arm64 libraspberrypi-bin arm64 1:1.20240902+1-1 [4,128 kB]
Get:2 http://archive.raspberrypi.com/debian bookworm/main arm64 libraspberrypi-dev arm64 1:1.20240902+1-1 [1,282 kB]
Get:3 http://archive.raspberrypi.com/debian bookworm/main arm64 raspberrypi-bootloader arm64 1:1.20240902+1-1 [13.0 MB]
Fetched 18.4 MB in 4s (4,384 kB/s)
(Reading database ... 87432 files and directories currently installed.)
Preparing to unpack .../libraspberrypi-bin_1.20240902+1-1_arm64.deb ...
Unpacking libraspberrypi-bin (1:1.20240902+1-1) ...
Setting up libraspberrypi-bin (1:1.20240902+1-1) ...
Processing triggers for man-db (2.11.2-2) ...
Done.
"""

# ---------------------------------------------------------------------------
# Restart gate — all endpoints return 503 while a simulated restart is running
# ---------------------------------------------------------------------------

@app.before_request
def check_restarting():
    if _restarting:
        return jsonify({'error': 'Agent is restarting'}), 503

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
        'hostname': _state['hostname'],
        'uptime_seconds': 86400 + random.randint(0, 3600),
        'version': _mock_version,
    })


@app.route('/system/info')
@require_token
def system_info():
    return jsonify({
        'cpu_percent':  round(random.uniform(5, 30), 1),
        'ram_percent':  round(random.uniform(30, 60), 1),
        'disk_percent': round(random.uniform(38, 45), 1),
        'cpu_temp':     round(random.uniform(46, 62), 1),
        'hostname':     _state['hostname'],
        'uptime_seconds': 86400 + random.randint(0, 3600),
    })


@app.route('/system/reboot', methods=['POST'])
@require_token
def reboot():
    print('[mock] Reboot requested — ignoring.')
    return jsonify({'message': 'Reboot requested (mock — no action taken)'})


@app.route('/system/agent-update', methods=['POST'])
@require_token
def agent_update():
    def _do_update():
        global _mock_version, _restarting
        _restarting = True
        print('[mock] Restarting for update…')
        time.sleep(5)  # simulate download + restart window
        try:
            r = requests.get(f'{FRAMEIT_SERVER}/api/agent/version', timeout=10)
            _mock_version = r.json().get('version', _mock_version)
        except Exception as e:
            print(f'[mock] Could not fetch server version: {e}')
        _restarting = False
        print(f'[mock] Back online — version {_mock_version}')
        try:
            if _frame_id:
                requests.post(
                    f'{FRAMEIT_SERVER}/api/agents/{_frame_id}/heartbeat',
                    json={'version': _mock_version},
                    timeout=5,
                )
        except Exception as e:
            print(f'[mock] Heartbeat after update failed: {e}')
    threading.Thread(target=_do_update, daemon=True).start()
    return jsonify({'message': 'Update started — agent will restart in a few seconds'})


@app.route('/system/update', methods=['POST'])
@require_token
def apt_update():
    time.sleep(1)  # simulate network latency
    return jsonify({'output': _APT_UPDATE_OUTPUT, 'returncode': 0})


@app.route('/system/upgrade', methods=['POST'])
@require_token
def apt_upgrade():
    time.sleep(2)
    return jsonify({'output': _APT_UPGRADE_OUTPUT, 'returncode': 0})


@app.route('/system/services')
@require_token
def service_status():
    return jsonify(_MOCK_SERVICES)


@app.route('/system/services/<name>/restart', methods=['POST'])
@require_token
def restart_service(name):
    if name not in _MOCK_SERVICES:
        return jsonify({'error': 'Unknown service'}), 400
    print(f'[mock] Restart {name}')
    return jsonify({'ok': True, 'output': ''})

# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

@app.route('/network/status')
@require_token
def network_status():
    return jsonify({
        'hostname': _state['hostname'],
        'interfaces': [
            {'name': 'eth0', 'ip': '192.168.1.42'},
            {'name': 'wlan0', 'ip': '192.168.1.43'},
        ],
        'ssid': _state['wifi_ssid'],
    })


@app.route('/network/wifi/scan')
@require_token
def wifi_scan():
    time.sleep(0.5)
    return jsonify({'networks': _MOCK_NETWORKS})


@app.route('/network/wifi/connect', methods=['POST'])
@require_token
def wifi_connect():
    body = request.get_json(silent=True) or {}
    ssid = body.get('ssid', '').strip()
    if not ssid:
        return jsonify({'error': 'ssid required'}), 400
    _state['wifi_ssid'] = ssid
    print(f'[mock] WiFi connect → {ssid}')
    return jsonify({'ok': True, 'output': f"Device 'wlan0' successfully activated with '{ssid}'"})

# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

@app.route('/display')
@require_token
def display_status():
    return jsonify({'on': _state['display_on']})


@app.route('/display/on', methods=['POST'])
@require_token
def display_on():
    _state['display_on'] = True
    print('[mock] Display ON')
    return jsonify({'ok': True})


@app.route('/display/off', methods=['POST'])
@require_token
def display_off():
    _state['display_on'] = False
    print('[mock] Display OFF')
    return jsonify({'ok': True})

# ---------------------------------------------------------------------------
# Registration + heartbeat
# ---------------------------------------------------------------------------

def register():
    global _frame_id
    while True:
        try:
            resp = requests.post(
                f'{FRAMEIT_SERVER}/api/agents/register',
                json={'token': FRAMEIT_TOKEN, 'hostname': _state['hostname'], 'port': AGENT_PORT},
                timeout=10,
            )
            if resp.status_code == 200:
                _frame_id = resp.json().get('frame_id')
                print(f'[mock] Registered as frame #{_frame_id}')
                try:
                    requests.post(
                        f'{FRAMEIT_SERVER}/api/agents/{_frame_id}/heartbeat',
                        json={'version': _mock_version},
                        timeout=5,
                    )
                except Exception:
                    pass
                return
            else:
                print(f'[mock] Registration failed ({resp.status_code}): {resp.text}')
        except Exception as e:
            print(f'[mock] Registration error: {e}')
        time.sleep(10)


def heartbeat_loop():
    while True:
        time.sleep(60)
        if not _frame_id:
            continue
        try:
            requests.post(
                f'{FRAMEIT_SERVER}/api/agents/{_frame_id}/heartbeat',
                json={'version': _mock_version},
                timeout=5,
            )
        except Exception:
            pass


if __name__ == '__main__':
    if not FRAMEIT_TOKEN:
        print('Usage: FRAMEIT_SERVER=http://localhost:5000 FRAMEIT_TOKEN=<token> python agent/mock_agent.py')
        raise SystemExit(1)

    print(f'[mock] Starting mock agent on port {AGENT_PORT}')
    print(f'[mock] Server: {FRAMEIT_SERVER}')
    print(f'[mock] Hostname: {_state["hostname"]}')

    threading.Thread(target=register, daemon=True).start()
    threading.Thread(target=heartbeat_loop, daemon=True).start()

    app.run(host='0.0.0.0', port=AGENT_PORT, debug=False)
