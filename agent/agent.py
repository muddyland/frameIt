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
import hmac
import os
import pwd
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
AGENT_BIND = os.environ.get('AGENT_BIND', '0.0.0.0')
KIOSK_USER = os.environ.get('KIOSK_USER', 'pi')
AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
SECRET_PATH = os.environ.get('AGENT_SECRET_PATH', os.path.join(AGENT_DIR, 'agent.secret'))
# Wrapper installed by the installer so sudo can be granted on a fixed command
# instead of `nmcli dev wifi connect *`, whose trailing wildcard matches any
# additional arguments.
WIFI_HELPER = '/usr/local/sbin/frameit-wifi-connect'

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
# Credentials
#
# The registration token used to double as the permanent bearer credential.
# The server now issues a per-frame secret at registration; the token is kept
# only as a fallback so an agent can still authenticate against a server that
# has not been upgraded yet.
# ---------------------------------------------------------------------------

_agent_secret = None
_secret_lock = threading.Lock()


def _load_secret():
    """Read the persisted agent secret, if one was issued previously."""
    global _agent_secret
    try:
        with open(SECRET_PATH, encoding='utf-8') as fh:
            value = fh.read().strip()
        if value:
            _agent_secret = value
            print('[agent] Loaded stored agent secret')
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f'[agent] Could not read {SECRET_PATH}: {exc}')


def _store_secret(value):
    """Adopt a newly issued secret and persist it 0600.

    Adoption happens even if the write fails — the running process must agree
    with the server about the current credential either way, and registration
    reissues one on the next restart.
    """
    global _agent_secret
    with _secret_lock:
        _agent_secret = value
    try:
        fd = os.open(SECRET_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(value)
    except OSError as exc:
        print(f'[agent] WARNING: could not persist agent secret: {exc}')


def _accepted_credentials():
    """Every bearer value this agent will currently accept."""
    with _secret_lock:
        current = _agent_secret
    return [c for c in (current, FRAMEIT_TOKEN) if c]

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
        if not auth.startswith('Bearer '):
            return jsonify({'error': 'Unauthorized'}), 401
        offered = auth[7:]
        # compare_digest on every candidate, without short-circuiting, so the
        # response time does not leak how much of the token matched.
        matched = False
        for candidate in _accepted_credentials():
            if hmac.compare_digest(offered, candidate):
                matched = True
        if not matched:
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
            # Ask the server what it expects the new agent to hash to, and
            # verify the download before writing it over ourselves. Without
            # this, anything that can answer for FRAMEIT_SERVER — trivial on a
            # plain-http LAN — gets persistent code execution on the Pi.
            expected = requests.get(f'{FRAMEIT_SERVER}/api/agent/version', timeout=15)
            expected.raise_for_status()
            expected_version = (expected.json() or {}).get('version')
            if not expected_version or expected_version == 'unknown':
                print('[agent] Update aborted: server did not publish an agent version')
                return

            r = requests.get(f'{FRAMEIT_SERVER}/agent.py', timeout=30)
            r.raise_for_status()
            payload = r.content
            actual_version = hashlib.sha256(payload).hexdigest()[:12]
            if not hmac.compare_digest(actual_version, expected_version):
                print(f'[agent] Update aborted: hash mismatch '
                      f'(expected {expected_version}, got {actual_version})')
                return
            if actual_version == AGENT_VERSION:
                print('[agent] Already up to date')
                return

            r_req = requests.get(f'{FRAMEIT_SERVER}/agent-requirements.txt', timeout=30)
            r_req.raise_for_status()

            # Write to a sibling file and replace atomically, so an interrupted
            # update cannot leave a half-written agent behind.
            tmp_path = agent_path + '.new'
            with open(tmp_path, 'wb') as f:
                f.write(payload)
            os.replace(tmp_path, agent_path)
            with open(req_path, 'w', encoding='utf-8') as f:
                f.write(r_req.text)
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
        proc = subprocess.Popen(  # pylint: disable=consider-using-with
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
        proc = subprocess.Popen(  # pylint: disable=consider-using-with
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

    # Prefer the fixed-arity helper the installer writes. The old sudoers rule
    # ended in a wildcard, which matches any trailing nmcli arguments, not
    # just an SSID and password. The helper also takes the passphrase on
    # stdin, keeping it out of the process table.
    if os.path.exists(WIFI_HELPER):
        cmd = ['sudo', WIFI_HELPER, ssid]
        stdin_data = f'{password}\n'
    else:
        # Agent updated ahead of the installer — fall back to the old path.
        cmd = ['sudo', 'nmcli', 'dev', 'wifi', 'connect', ssid]
        if password:
            cmd += ['password', password]
        stdin_data = None

    try:
        r = subprocess.run(cmd, input=stdin_data, capture_output=True,
                           text=True, timeout=45)
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Timed out connecting to the network.'}), 504
    if r.returncode != 0:
        return jsonify({'error': (r.stderr or r.stdout or 'Connection failed').strip()}), 500
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
# Registration + heartbeat
# ---------------------------------------------------------------------------

def register():
    """POSTs to the FrameIT server to register this agent. Retries indefinitely."""
    global _frame_id
    backoff = 15
    while True:
        try:
            resp = requests.post(
                f'{FRAMEIT_SERVER}/api/agents/register',
                json={
                    'token': FRAMEIT_TOKEN,
                    'hostname': socket.gethostname(),
                    'port': AGENT_PORT,
                    # Tells an upgraded server to issue a dedicated credential.
                    # Older servers ignore this and keep using the token.
                    'supports_secret': True,
                },
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                _frame_id = data.get('frame_id')
                issued = data.get('agent_secret')
                if issued:
                    _store_secret(issued)
                    print(f'[agent] Registered as frame #{_frame_id} with a dedicated credential')
                else:
                    print(f'[agent] Registered as frame #{_frame_id} (server issued no secret; '
                          f'falling back to the registration token)')
                return
            print(f'[agent] Registration failed ({resp.status_code}): {resp.text[:200]}')
        except Exception as e:
            print(f'[agent] Registration error: {e}')
        time.sleep(backoff)
        backoff = min(backoff * 2, 300)


def _auth_headers():
    creds = _accepted_credentials()
    return {'Authorization': f'Bearer {creds[0]}'} if creds else {}


def heartbeat_loop():
    """Sends a heartbeat to the FrameIT server every 60 seconds."""
    while True:
        time.sleep(60)
        if not _frame_id:
            continue
        try:
            resp = requests.post(
                f'{FRAMEIT_SERVER}/api/agents/{_frame_id}/heartbeat',
                json={'version': AGENT_VERSION},
                headers=_auth_headers(),
                timeout=5,
            )
            # The server rotates the secret on every registration, so a 401
            # means our copy is stale — re-register rather than going silent.
            if resp.status_code == 401:
                print('[agent] Heartbeat rejected; re-registering')
                register()
        except Exception:
            pass


if __name__ == '__main__':
    if not FRAMEIT_SERVER or not FRAMEIT_TOKEN:
        print('[agent] FRAMEIT_SERVER and FRAMEIT_TOKEN must be set.')
        raise SystemExit(1)

    _load_secret()

    threading.Thread(target=register, daemon=True).start()
    threading.Thread(target=heartbeat_loop, daemon=True).start()

    app.run(host=AGENT_BIND, port=AGENT_PORT, threaded=True)
