import hashlib
import os
import re
import uuid
import random
import secrets
import subprocess

import requests as http_requests
from flask import (Flask, render_template, request, jsonify, send_from_directory,
                   Response, stream_with_context, session, redirect, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from models import db, Poster, Trailer, Frame, FrameLog, RegistrationToken, Settings, AdminUser, utcnow

STATIC_DIR = './static'
DATA_DIR = os.environ.get("DATA_DIR", './config')
IMAGES_DIR = os.environ.get("IMAGES_DIR", './images')

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(IMAGES_DIR, exist_ok=True)
os.makedirs(os.path.join(STATIC_DIR, 'dist'), exist_ok=True)

app = Flask(__name__, static_url_path='/static', static_folder=STATIC_DIR)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(os.path.abspath(DATA_DIR), 'frameit.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Persist secret key so sessions survive restarts
_key_path = os.path.join(DATA_DIR, 'secret.key')
if os.path.exists(_key_path):
    with open(_key_path, encoding='utf-8') as _f:
        _secret = _f.read().strip()
else:
    _secret = secrets.token_hex(32)
    with open(_key_path, 'w', encoding='utf-8') as _f:
        _f.write(_secret)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', _secret)

db.init_app(app)

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_PUBLIC_ENDPOINTS = {
    'frame', 'manifest', 'frame_checkin', 'frame_next', 'frame_signal',
    'agent_register', 'agent_heartbeat', 'agent_server_version', 'install_script',
    'serve_agent', 'serve_agent_requirements', 'send_images',
    'admin_login', 'admin_logout', 'admin_setup', 'static',
}


@app.before_request
def check_auth():
    if request.endpoint is None or request.endpoint in _PUBLIC_ENDPOINTS:
        return None
    # Already authenticated — fast path, no DB query
    if session.get('admin_user'):
        return None
    # First-run: no users exist yet
    if AdminUser.query.count() == 0:
        return redirect(url_for('admin_setup'))
    # Not logged in
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Unauthorized'}), 401
    return redirect(url_for('admin_login', next=request.path))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_youtube_id(raw):
    """Accept a full YouTube URL or a bare 11-character video ID."""
    for pat in [r'(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})', r'^([A-Za-z0-9_-]{11})$']:
        m = re.search(pat, raw.strip())
        if m:
            return m.group(1)
    return None


def allowed_image(filename):
    return filename.rsplit('.', 1)[-1].lower() in {'jpg', 'jpeg', 'png', 'webp'}


def get_settings():
    """Return the singleton Settings row, creating it with defaults if absent."""
    s = db.session.get(Settings, 1)
    if not s:
        s = Settings(id=1, default_title_above='Now Playing', default_title_below='',
                     default_interval_seconds=300)
        db.session.add(s)
        db.session.commit()
    return s


# ---------------------------------------------------------------------------
# Frame display
# ---------------------------------------------------------------------------

@app.route('/')
def frame():
    return render_template('frame.html')


@app.route('/manifest.json')
def manifest():
    return jsonify({
        "name": "FrameIT",
        "short_name": "FrameIT",
        "description": "Digital photo-frame manager for Raspberry Pi kiosks.",
        "start_url": "/admin",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait-primary",
        "background_color": "#000000",
        "theme_color": "#000000",
        "icons": [
            {
                "src": "/static/img/icon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any maskable",
            },
        ],
    })


# ---------------------------------------------------------------------------
# Frame API — checkin + next content
# ---------------------------------------------------------------------------

@app.route('/api/frames/checkin', methods=['POST'])
def frame_checkin():
    body = request.get_json(silent=True) or {}
    ip = request.remote_addr
    bypass = body.get('bypass', False)

    frame = Frame.query.filter_by(ip=ip).first()

    if not frame:
        if bypass:
            # Preview/bypass mode — create a temporary anonymous frame
            settings = get_settings()
            pinned = settings.default_content_mode == 'pinned'
            frame = Frame(ip=ip, name=f'[Preview] {body.get("hostname", ip)}',
                          interval_seconds=settings.default_interval_seconds,
                          rotation=settings.default_rotation,
                          content_mode=settings.default_content_mode,
                          pinned_type=settings.default_pinned_type if pinned else None,
                          pinned_id=settings.default_pinned_id if pinned else None)
            db.session.add(frame)
            db.session.commit()
        else:
            return jsonify({'registered': False})

    frame.last_seen = utcnow()
    db.session.commit()
    return jsonify({
        'registered': True,
        'frame_id': frame.id,
        'interval_seconds': frame.interval_seconds,
        'rotation': frame.rotation,
    })


@app.route('/api/frames/<int:frame_id>/next', methods=['GET'])
def frame_next(frame_id):
    frame = Frame.query.get_or_404(frame_id)
    frame.last_seen = utcnow()
    db.session.commit()

    content = None

    if frame.content_mode == 'pinned' and frame.pinned_type and frame.pinned_id:
        if frame.pinned_type == 'poster':
            content = Poster.query.filter_by(id=frame.pinned_id, active=True).first()
            if content:
                content = ('poster', content)
        elif frame.pinned_type == 'trailer':
            content = Trailer.query.filter_by(id=frame.pinned_id, active=True).first()
            if content:
                content = ('trailer', content)

    if not content:
        # Pool mode — collect all active items and pick one randomly
        posters = [(Poster, p) for p in Poster.query.filter_by(active=True).all()]
        trailers = [(Trailer, t) for t in Trailer.query.filter_by(active=True).all()]
        pool = [('poster', p) for _, p in posters] + [('trailer', t) for _, t in trailers]
        if pool:
            content = random.choice(pool)

    if not content:
        return jsonify({'type': 'empty', 'rotation': frame.rotation, 'interval_seconds': frame.interval_seconds})

    content_type, item = content

    log = FrameLog(frame_id=frame.id, content_type=content_type, content_id=item.id)
    db.session.add(log)
    db.session.commit()

    base = {'rotation': frame.rotation, 'interval_seconds': frame.interval_seconds}
    if content_type == 'poster':
        settings = get_settings()
        title_above = item.title_above if item.title_above is not None else settings.default_title_above
        title_below = item.title_below if item.title_below is not None else settings.default_title_below
        return jsonify({**base, 'type': 'poster', 'id': item.id,
                        'url': f'/images/{item.filename}',
                        'title_above': title_above or '',
                        'title_below': title_below or ''})
    return jsonify({**base, 'type': 'trailer', 'id': item.id,
                    'youtube_id': item.youtube_id, 'title': item.title})


# ---------------------------------------------------------------------------
# Frame signal / command
# ---------------------------------------------------------------------------

@app.route('/api/frames/<int:frame_id>/signal')
def frame_signal(frame_id):
    """Polled by the frame client every few seconds to receive commands."""
    frame = Frame.query.get_or_404(frame_id)
    cmd = frame.pending_command
    if cmd:
        frame.pending_command = None
        db.session.commit()
    return jsonify({'command': cmd})


@app.route('/api/frames/<int:frame_id>/command', methods=['POST'])
def frame_send_command(frame_id):
    frame = Frame.query.get_or_404(frame_id)
    body = request.get_json(silent=True) or {}
    cmd = body.get('command', '').strip()
    if cmd not in ('next', 'refresh'):
        return jsonify({'error': 'Invalid command'}), 400
    frame.pending_command = cmd
    db.session.commit()
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Poster API
# ---------------------------------------------------------------------------

@app.route('/api/posters', methods=['GET'])
def get_posters():
    posters = Poster.query.order_by(Poster.sort_order, Poster.created_at).all()
    return jsonify([p.to_dict() for p in posters])


@app.route('/api/posters/upload', methods=['POST'])
def upload_poster():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    f = request.files['file']
    if not f.filename or not allowed_image(f.filename):
        return jsonify({'error': 'File must be jpg, jpeg, png, or webp'}), 400

    filename = f'{uuid.uuid4().hex}_{secure_filename(f.filename)}'
    f.save(os.path.join(IMAGES_DIR, filename))

    poster = Poster(
        filename=filename,
        title_above=request.form.get('title_above', '').strip() or None,
        title_below=request.form.get('title_below', '').strip() or None,
        active=request.form.get('active', 'true').lower() != 'false',
    )
    db.session.add(poster)
    db.session.commit()
    return jsonify(poster.to_dict()), 201


@app.route('/api/posters/<int:poster_id>', methods=['PATCH'])
def update_poster(poster_id):
    poster = Poster.query.get_or_404(poster_id)
    body = request.get_json(silent=True) or {}
    if 'title_above' in body:
        poster.title_above = body['title_above'].strip() or None
    if 'title_below' in body:
        poster.title_below = body['title_below'].strip() or None
    if 'active' in body:
        poster.active = bool(body['active'])
    if 'sort_order' in body:
        poster.sort_order = int(body['sort_order'])
    db.session.commit()
    return jsonify(poster.to_dict())


@app.route('/api/posters/<int:poster_id>', methods=['DELETE'])
def delete_poster(poster_id):
    poster = Poster.query.get_or_404(poster_id)
    filepath = os.path.join(IMAGES_DIR, poster.filename)
    if os.path.exists(filepath):
        os.remove(filepath)
    db.session.delete(poster)
    db.session.commit()
    return '', 204


# ---------------------------------------------------------------------------
# Trailer API
# ---------------------------------------------------------------------------

@app.route('/api/trailers', methods=['GET'])
def get_trailers():
    trailers = Trailer.query.order_by(Trailer.created_at).all()
    return jsonify([t.to_dict() for t in trailers])


@app.route('/api/trailers', methods=['POST'])
def add_trailer():
    body = request.get_json(silent=True) or {}
    raw_url = body.get('url', '').strip()
    title = body.get('title', '').strip()
    if not raw_url or not title:
        return jsonify({'error': 'url and title are required'}), 400

    youtube_id = parse_youtube_id(raw_url)
    if not youtube_id:
        return jsonify({'error': 'Could not parse a YouTube video ID from the provided URL'}), 400

    existing = Trailer.query.filter_by(youtube_id=youtube_id).first()
    if existing:
        return jsonify(existing.to_dict()), 200

    trailer = Trailer(youtube_id=youtube_id, title=title)
    db.session.add(trailer)
    db.session.commit()
    return jsonify(trailer.to_dict()), 201


@app.route('/api/trailers/<int:trailer_id>', methods=['PATCH'])
def update_trailer(trailer_id):
    trailer = Trailer.query.get_or_404(trailer_id)
    body = request.get_json(silent=True) or {}
    if 'title' in body:
        trailer.title = body['title'].strip()
    if 'active' in body:
        trailer.active = bool(body['active'])
    db.session.commit()
    return jsonify(trailer.to_dict())


@app.route('/api/trailers/<int:trailer_id>', methods=['DELETE'])
def delete_trailer(trailer_id):
    trailer = Trailer.query.get_or_404(trailer_id)
    db.session.delete(trailer)
    db.session.commit()
    return '', 204


# ---------------------------------------------------------------------------
# Frame admin API
# ---------------------------------------------------------------------------

@app.route('/api/frames', methods=['GET'])
def get_frames():
    frames = Frame.query.order_by(Frame.name).all()
    return jsonify([f.to_dict() for f in frames])


@app.route('/api/frames/<int:frame_id>', methods=['GET'])
def get_frame(frame_id):
    return jsonify(Frame.query.get_or_404(frame_id).to_dict())


@app.route('/api/frames/cleanup', methods=['POST'])
def cleanup_frames():
    unregistered = Frame.query.filter_by(agent_url=None).all()
    count = len(unregistered)
    for frame in unregistered:
        FrameLog.query.filter_by(frame_id=frame.id).delete()
        RegistrationToken.query.filter_by(frame_id=frame.id).update({'frame_id': None})
        db.session.delete(frame)
    db.session.commit()
    return jsonify({'removed': count})


@app.route('/api/frames/<int:frame_id>', methods=['DELETE'])
def delete_frame(frame_id):
    frame = Frame.query.get_or_404(frame_id)
    FrameLog.query.filter_by(frame_id=frame.id).delete()
    RegistrationToken.query.filter_by(frame_id=frame.id).update({'frame_id': None})
    db.session.delete(frame)
    db.session.commit()
    return '', 204


@app.route('/api/frames/<int:frame_id>', methods=['PATCH'])
def update_frame(frame_id):
    frame = Frame.query.get_or_404(frame_id)
    body = request.get_json(silent=True) or {}
    if 'name' in body:
        frame.name = body['name'].strip() or None
    if 'rotation' in body:
        if body['rotation'] not in (0, 90, 180, 270):
            return jsonify({'error': 'rotation must be 0, 90, 180, or 270'}), 400
        frame.rotation = body['rotation']
    if 'interval_seconds' in body:
        frame.interval_seconds = max(10, int(body['interval_seconds']))
    if 'content_mode' in body:
        if body['content_mode'] not in ('pool', 'pinned'):
            return jsonify({'error': 'content_mode must be pool or pinned'}), 400
        frame.content_mode = body['content_mode']
    if 'pinned_type' in body:
        frame.pinned_type = body['pinned_type'] or None
    if 'pinned_id' in body:
        frame.pinned_id = body['pinned_id'] or None
    db.session.commit()
    return jsonify(frame.to_dict())


# ---------------------------------------------------------------------------
# Agent registration + proxy
# ---------------------------------------------------------------------------

@app.route('/api/tokens', methods=['GET'])
def list_tokens():
    tokens = RegistrationToken.query.order_by(RegistrationToken.created_at.desc()).all()
    base_url = request.host_url.rstrip('/')
    result = []
    for t in tokens:
        result.append({
            'id': t.id,
            'token': t.token,
            'created_at': t.created_at.isoformat(),
            'used_at': t.used_at.isoformat() if t.used_at else None,
            'frame_id': t.frame_id,
            'install_cmd': f"curl -sSL {base_url}/install.sh | sudo bash -s -- --server {base_url} --token {t.token}",
        })
    return jsonify(result)


@app.route('/api/tokens', methods=['POST'])
def create_token():
    token_value = secrets.token_hex(32)
    token = RegistrationToken(token=token_value)
    db.session.add(token)
    db.session.commit()
    base_url = request.host_url.rstrip('/')
    return jsonify({
        'id': token.id,
        'token': token.token,
        'created_at': token.created_at.isoformat(),
        'install_cmd': f"curl -sSL {base_url}/install.sh | sudo bash -s -- --server {base_url} --token {token.token}",
    }), 201


@app.route('/api/tokens/<int:token_id>', methods=['DELETE'])
def delete_token(token_id):
    token = RegistrationToken.query.get_or_404(token_id)
    if token.used_at:
        return jsonify({'error': 'Cannot revoke a token that has already been used'}), 400
    db.session.delete(token)
    db.session.commit()
    return '', 204


@app.route('/api/agents/register', methods=['POST'])
def agent_register():
    body = request.get_json(silent=True) or {}
    token_value = body.get('token', '').strip()
    hostname = body.get('hostname', '')
    port = body.get('port', 5001)

    token = RegistrationToken.query.filter_by(token=token_value).first()
    if not token:
        return jsonify({'error': 'Invalid token'}), 401

    ip = request.remote_addr

    if token.used_at:
        # Allow re-registration from the same frame (e.g. after agent update/restart).
        # Any other IP gets rejected so the token can't be hijacked.
        if not token.frame_id:
            return jsonify({'error': 'Token already used'}), 401
        frame = Frame.query.get(token.frame_id)
        if not frame or frame.ip != ip:
            return jsonify({'error': 'Token already used'}), 401
        frame.agent_url = f'http://{ip}:{port}'
        frame.agent_last_seen = utcnow()
        db.session.commit()
        return jsonify({'frame_id': frame.id, 'ok': True})
    agent_url = f'http://{ip}:{port}'

    frame = Frame.query.filter_by(ip=ip).first()
    if not frame:
        settings = get_settings()
        pinned = settings.default_content_mode == 'pinned'
        frame = Frame(ip=ip, name=hostname,
                      interval_seconds=settings.default_interval_seconds,
                      rotation=settings.default_rotation,
                      content_mode=settings.default_content_mode,
                      pinned_type=settings.default_pinned_type if pinned else None,
                      pinned_id=settings.default_pinned_id if pinned else None)
        db.session.add(frame)
    frame.agent_url = agent_url
    frame.agent_token = token_value
    frame.agent_last_seen = utcnow()
    if not frame.name:
        frame.name = hostname

    token.used_at = utcnow()
    db.session.flush()
    token.frame_id = frame.id
    db.session.commit()
    return jsonify({'frame_id': frame.id, 'ok': True})


@app.route('/api/agents/<int:frame_id>/heartbeat', methods=['POST'])
def agent_heartbeat(frame_id):
    frame = Frame.query.get_or_404(frame_id)
    frame.agent_last_seen = utcnow()
    body = request.get_json(silent=True) or {}
    if 'version' in body:
        frame.agent_version = body['version']
    db.session.commit()
    return jsonify({'interval_seconds': frame.interval_seconds, 'rotation': frame.rotation})


@app.route('/api/agent/version')
def agent_server_version():
    path = os.path.join(os.path.dirname(__file__), 'agent', 'agent.py')
    try:
        with open(path, 'rb') as f:
            version = hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError:
        version = 'unknown'
    return jsonify({'version': version})


@app.route('/api/frames/<int:frame_id>/agent/<path:subpath>', methods=['GET', 'POST', 'PATCH', 'DELETE'])
def agent_proxy(frame_id, subpath):
    frame = Frame.query.get_or_404(frame_id)
    if not frame.agent_url:
        return jsonify({'error': 'No agent registered for this frame'}), 404

    target = f"{frame.agent_url}/{subpath}"
    headers = {'Authorization': f'Bearer {frame.agent_token}', 'Content-Type': 'application/json'}

    try:
        resp = http_requests.request(
            method=request.method,
            url=target,
            headers=headers,
            json=request.get_json(silent=True),
            stream=True,
            timeout=(10, 300),  # 10s connect, 5min read between chunks
        )
        # Forward streaming-friendly headers so nginx doesn't buffer
        fwd_headers = {}
        for h in ('X-Accel-Buffering', 'Cache-Control'):
            if h in resp.headers:
                fwd_headers[h] = resp.headers[h]
        ct = resp.headers.get('Content-Type', 'application/json')
        # iter_content(chunk_size=None) calls raw.read(None) which blocks until
        # the entire response is received — useless for streaming.  For plain-
        # text streaming responses (apt output etc.) use iter_lines() instead so
        # each line is forwarded as soon as it arrives.
        if ct.startswith('text/plain'):
            def _stream_lines():
                for line in resp.iter_lines(decode_unicode=True):
                    yield line + '\n'
            body = stream_with_context(_stream_lines())
        else:
            body = stream_with_context(resp.iter_content(chunk_size=512))
        return Response(body, status=resp.status_code, content_type=ct,
                        headers=fwd_headers)
    except (http_requests.exceptions.ConnectionError, http_requests.exceptions.Timeout):
        return jsonify({'error': 'Agent unreachable'}), 503


# ---------------------------------------------------------------------------
# Install script
# ---------------------------------------------------------------------------

@app.route('/install.sh')
def install_script():
    base_url = request.host_url.rstrip('/')
    return Response(
        render_template('install.sh', base_url=base_url),
        mimetype='text/plain',
    )


@app.route('/agent.py')
def serve_agent():
    return send_from_directory('agent', 'agent.py', mimetype='text/plain')


@app.route('/agent-requirements.txt')
def serve_agent_requirements():
    return send_from_directory('agent', 'requirements.txt', mimetype='text/plain')


# ---------------------------------------------------------------------------
# Settings API
# ---------------------------------------------------------------------------

@app.route('/api/settings', methods=['GET'])
def get_settings_api():
    return jsonify(get_settings().to_dict())


@app.route('/api/settings', methods=['PATCH'])
def update_settings():
    s = get_settings()
    body = request.get_json(silent=True) or {}
    if 'default_title_above' in body:
        s.default_title_above = body['default_title_above'].strip() or None
    if 'default_title_below' in body:
        s.default_title_below = body['default_title_below'].strip() or None
    if 'default_interval_seconds' in body:
        s.default_interval_seconds = max(10, int(body['default_interval_seconds']))
    if 'default_rotation' in body:
        if body['default_rotation'] not in (0, 90, 180, 270):
            return jsonify({'error': 'rotation must be 0, 90, 180, or 270'}), 400
        s.default_rotation = body['default_rotation']
    if 'default_content_mode' in body:
        if body['default_content_mode'] not in ('pool', 'pinned'):
            return jsonify({'error': 'content_mode must be pool or pinned'}), 400
        s.default_content_mode = body['default_content_mode']
    if 'default_pinned_type' in body:
        v = body['default_pinned_type']
        if v and v not in ('poster', 'trailer'):
            return jsonify({'error': 'pinned_type must be poster or trailer'}), 400
        s.default_pinned_type = v or None
    if 'default_pinned_id' in body:
        s.default_pinned_id = int(body['default_pinned_id']) if body['default_pinned_id'] else None
    db.session.commit()
    return jsonify(s.to_dict())


# ---------------------------------------------------------------------------
# Admin auth routes
# ---------------------------------------------------------------------------

@app.route('/admin/setup', methods=['GET', 'POST'])
def admin_setup():
    # Only accessible when no users exist
    if AdminUser.query.count() > 0:
        return redirect(url_for('admin_index'))
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        confirm  = request.form.get('confirm', '')
        if not username or not password:
            error = 'Username and password are required.'
        elif password != confirm:
            error = 'Passwords do not match.'
        else:
            user = AdminUser(username=username, password_hash=generate_password_hash(password))
            db.session.add(user)
            db.session.commit()
            session['admin_user'] = username
            return redirect(url_for('admin_index'))
    return render_template('admin_setup.html', error=error)


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if session.get('admin_user'):
        return redirect(url_for('admin_index'))
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = AdminUser.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            session['admin_user'] = username
            return redirect(request.args.get('next') or url_for('admin_index'))
        error = 'Invalid username or password.'
    return render_template('admin_login.html', error=error)


@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_user', None)
    return redirect(url_for('admin_login'))


@app.route('/admin/password', methods=['POST'])
def admin_change_password():
    username = session.get('admin_user')
    if not username:
        return jsonify({'error': 'Unauthorized'}), 401
    body = request.get_json(silent=True) or {}
    current  = body.get('current', '')
    new_pass = body.get('new', '').strip()
    user = AdminUser.query.filter_by(username=username).first()
    if not user or not check_password_hash(user.password_hash, current):
        return jsonify({'error': 'Current password is incorrect.'}), 400
    if not new_pass:
        return jsonify({'error': 'New password cannot be empty.'}), 400
    user.password_hash = generate_password_hash(new_pass)
    db.session.commit()
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Admin UI routes
# ---------------------------------------------------------------------------

@app.route('/admin')
def admin_index():
    return render_template('index.html')


@app.route('/admin/posters')
def admin_posters():
    return render_template('posters.html')


@app.route('/admin/trailers')
def admin_trailers():
    return render_template('trailers.html')


@app.route('/admin/frames')
def admin_frames():
    return render_template('admin_frames.html')


@app.route('/admin/settings')
def admin_settings():
    return render_template('admin_settings.html')


@app.route('/admin/tokens')
def admin_tokens():
    return redirect(url_for('admin_frames'))


# ---------------------------------------------------------------------------
# Static file serving
# ---------------------------------------------------------------------------

@app.route('/images/<path:path>')
def send_images(path):
    return send_from_directory(IMAGES_DIR, path)


# ---------------------------------------------------------------------------
# DB init + schema migrations
# ---------------------------------------------------------------------------

# Each entry is (table, column, column_definition).
# Add a new row here whenever a column is added to a model.
_MIGRATIONS = [
    ('frame',    'agent_version',             'VARCHAR(12)'),
    ('settings', 'default_interval_seconds',  'INTEGER NOT NULL DEFAULT 300'),
    ('settings', 'default_rotation',          'INTEGER NOT NULL DEFAULT 0'),
    ('settings', 'default_content_mode',      "VARCHAR(10) NOT NULL DEFAULT 'pool'"),
    ('settings', 'default_pinned_type',        'VARCHAR(10)'),
    ('settings', 'default_pinned_id',          'INTEGER'),
    ('frame',    'pending_command',             'VARCHAR(20)'),
]


def migrate_schema():
    """Apply any pending column additions — safe to call on every startup."""
    with db.engine.connect() as conn:
        for table, column, definition in _MIGRATIONS:
            rows = conn.execute(db.text(f'PRAGMA table_info("{table}")')).fetchall()
            existing = {r[1] for r in rows}
            if column not in existing:
                conn.execute(db.text(f'ALTER TABLE "{table}" ADD COLUMN {column} {definition}'))
                conn.commit()
                print(f'[db] Added column {table}.{column}')


@app.cli.command('init-db')
def init_db_command():
    db.create_all()
    migrate_schema()
    print('Database initialized.')


with app.app_context():
    db.create_all()
    migrate_schema()


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
