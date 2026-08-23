# pylint: disable=too-many-lines
import hashlib
import hmac
import logging
import os
import queue
import random
import re
import secrets
import shutil
import stat
import threading
import time
import typing
import uuid
from contextlib import closing
from datetime import timedelta

from sqlalchemy import event as sa_event
from sqlalchemy.engine import Engine as SAEngine
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.exc import OperationalError as SAOperationalError

import requests as http_requests
from flask import (Flask, render_template, request, jsonify, send_from_directory,
                   Response, stream_with_context, session, redirect, url_for, abort)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from models import db, Poster, Trailer, Frame, FrameLog, RegistrationToken, Settings, AdminUser, utcnow

try:
    import yt_dlp as _yt_dlp
    _YT_DLP_AVAILABLE = True
except ImportError:  # pragma: no cover
    _YT_DLP_AVAILABLE = False

_log = logging.getLogger(__name__)

# If ffmpeg is present we can merge separate video+audio streams for 720p quality.
# Without it we fall back to the best pre-merged stream (usually ≤480p).
_FFMPEG_AVAILABLE = shutil.which('ffmpeg') is not None

STATIC_DIR = './static'
DATA_DIR = os.environ.get("DATA_DIR", './config')
IMAGES_DIR = os.environ.get("IMAGES_DIR", './images')
VIDEOS_DIR = os.environ.get("VIDEOS_DIR", './videos')

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(IMAGES_DIR, exist_ok=True)
os.makedirs(VIDEOS_DIR, exist_ok=True)
os.makedirs(os.path.join(STATIC_DIR, 'dist'), exist_ok=True)

MAX_UPLOAD_BYTES = int(os.environ.get('MAX_UPLOAD_MB', '16')) * 1024 * 1024
# Lets the test suite and one-shot CLI commands run without a download worker
# and maintenance sweep racing them in the background.
DISABLE_WORKERS = os.environ.get('FRAMEIT_DISABLE_WORKERS', '').lower() in ('1', 'true', 'yes')
# Number of trusted reverse-proxy hops. Existing deployments run behind the
# nginx config in the README, so this defaults to 1 to preserve their frame
# identities. Set to 0 when gunicorn is exposed directly — otherwise clients
# can forge X-Forwarded-For and choose their own remote_addr.
TRUSTED_PROXY_HOPS = int(os.environ.get('TRUSTED_PROXY_HOPS', '1'))

app = Flask(__name__, static_url_path='/static', static_folder=STATIC_DIR)
if TRUSTED_PROXY_HOPS > 0:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=TRUSTED_PROXY_HOPS, x_proto=TRUSTED_PROXY_HOPS,
                            x_host=TRUSTED_PROXY_HOPS, x_prefix=TRUSTED_PROXY_HOPS)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(os.path.abspath(DATA_DIR), 'frameit.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_BYTES
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
# Only set the Secure flag when the deployment is actually on HTTPS — forcing
# it on a plain-http LAN install would silently discard the session cookie.
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('FORCE_HTTPS', '').lower() in ('1', 'true', 'yes')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)


def _read_or_create_secret_key(path):
    """Load the persisted session key, creating it 0600 if absent.

    Older releases wrote this file with the default umask, so an existing key
    is tightened in place on startup rather than being regenerated — rotating
    it would log every admin out on upgrade.
    """
    if os.path.exists(path):
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
            if mode & 0o077:
                os.chmod(path, 0o600)
                _log.warning('[security] Tightened permissions on %s (was %o)', path, mode)
        except OSError as exc:  # pragma: no cover - platform dependent
            _log.warning('[security] Could not chmod %s: %s', path, exc)
        with open(path, encoding='utf-8') as fh:
            existing = fh.read().strip()
        if existing:
            return existing
    value = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        fh.write(value)
    return value


_key_path = os.path.join(DATA_DIR, 'secret.key')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or _read_or_create_secret_key(_key_path)

db.init_app(app)


# ---------------------------------------------------------------------------
# SQLite tuning
#
# The default rollback journal serialises writers with a coarse lock and gives
# up immediately on contention. Every frame commits on checkin, on each /next,
# and whenever /signal clears a command — with more than one gunicorn worker
# that reliably produces "database is locked". WAL plus a busy timeout lets
# readers and the writer proceed concurrently and makes brief contention wait
# instead of fail.
# ---------------------------------------------------------------------------

@sa_event.listens_for(SAEngine, 'connect')
def _sqlite_pragmas(dbapi_conn, _record):
    if type(dbapi_conn).__module__.split('.', maxsplit=1)[0] not in ('sqlite3', 'pysqlite2'):
        return
    cur = dbapi_conn.cursor()
    try:
        cur.execute('PRAGMA journal_mode=WAL')
        cur.execute('PRAGMA busy_timeout=5000')
        cur.execute('PRAGMA synchronous=NORMAL')
        cur.execute('PRAGMA foreign_keys=ON')
    finally:
        cur.close()

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

# Endpoints reachable without an admin session. Frames and agents are not
# browsers and authenticate with their own credentials instead.
_PUBLIC_ENDPOINTS = {
    'frame', 'manifest', 'frame_checkin', 'frame_next', 'frame_signal',
    'frame_display_state', 'healthz',
    'agent_register', 'agent_heartbeat', 'agent_server_version', 'install_script',
    'serve_agent', 'serve_agent_requirements', 'send_images',
    'admin_login', 'admin_logout', 'admin_setup', 'static', 'serve_video',
}

# State-changing endpoints that are NOT authenticated by the session cookie,
# so a CSRF token would be meaningless. Everything else that mutates state
# must present one.
_CSRF_EXEMPT = {
    'frame_checkin', 'frame_next', 'frame_signal', 'frame_display_state',
    'agent_register', 'agent_heartbeat',
}

_CSRF_SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS', 'TRACE'}
_CSRF_HEADER = 'X-CSRF-Token'
_CSRF_FIELD = '_csrf_token'


def csrf_token():
    """Return this session's CSRF token, minting one on first use."""
    token = session.get('_csrf')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf'] = token
    return token


app.jinja_env.globals['csrf_token'] = csrf_token


def _csrf_valid():
    expected = session.get('_csrf')
    if not expected:
        return False
    sent = request.headers.get(_CSRF_HEADER) or request.form.get(_CSRF_FIELD)
    if not sent:
        body = request.get_json(silent=True)
        if isinstance(body, dict):
            sent = body.get(_CSRF_FIELD)
    return bool(sent) and hmac.compare_digest(str(sent), str(expected))


def _password_fingerprint(password_hash):
    """Short digest of a stored hash, used to invalidate sessions on change."""
    return hashlib.sha256(password_hash.encode('utf-8')).hexdigest()[:16]


def _current_admin():
    """The AdminUser for this session, or None if the session is stale.

    Costs one indexed lookup per authenticated request. That is the price of
    having a password change actually log other sessions out — the previous
    fast path trusted the cookie alone, so a stolen session outlived any
    number of password rotations.
    """
    username = session.get('admin_user')
    if not username:
        return None
    user = AdminUser.query.filter_by(username=username).first()
    if not user:
        return None
    if session.get('admin_fp') != _password_fingerprint(user.password_hash):
        return None
    return user


def _wants_json():
    return request.path.startswith('/api/') or request.accept_mimetypes.best == 'application/json'


@app.before_request
def check_auth():  # pylint: disable=too-many-return-statements
    endpoint = request.endpoint
    if endpoint is None:
        return None

    if endpoint not in _PUBLIC_ENDPOINTS:
        if _current_admin() is None:
            session.pop('admin_user', None)
            session.pop('admin_fp', None)
            if AdminUser.query.count() == 0:
                return redirect(url_for('admin_setup'))
            if _wants_json():
                return jsonify({'error': 'Unauthorized'}), 401
            return redirect(url_for('admin_login', next=request.path))

    if request.method not in _CSRF_SAFE_METHODS and endpoint not in _CSRF_EXEMPT:
        if not _csrf_valid():
            _log.warning('[security] CSRF rejection on %s %s', request.method, request.path)
            if _wants_json():
                return jsonify({'error': 'CSRF token missing or invalid. Reload the page and try again.'}), 400
            return render_template('admin_login.html',
                                   error='Your session expired. Please sign in again.'), 400
    return None


# ---------------------------------------------------------------------------
# Login throttling
#
# Counters are per-process, so with N gunicorn workers the effective ceiling
# is N x LOGIN_MAX_ATTEMPTS. That is still a hard brake on the unlimited
# guessing the login form allowed before, without putting a database write on
# the authentication path.
# ---------------------------------------------------------------------------

LOGIN_MAX_ATTEMPTS = 10
LOGIN_WINDOW_SECONDS = 300
LOGIN_LOCKOUT_SECONDS = 300
MIN_PASSWORD_LENGTH = 8

_login_attempts: dict = {}
_login_attempts_lock = threading.Lock()


def _login_blocked(key):
    """Seconds remaining on a lockout, or 0 if the caller may attempt a login."""
    now = time.monotonic()
    with _login_attempts_lock:
        record = _login_attempts.get(key)
        if not record:
            return 0
        count, first_seen, locked_until = record
        if locked_until > now:
            return int(locked_until - now) + 1
        if now - first_seen > LOGIN_WINDOW_SECONDS:
            _login_attempts.pop(key, None)
            return 0
        if count >= LOGIN_MAX_ATTEMPTS:
            _login_attempts[key] = (count, first_seen, now + LOGIN_LOCKOUT_SECONDS)
            return LOGIN_LOCKOUT_SECONDS
    return 0


def _record_login_failure(key):
    now = time.monotonic()
    with _login_attempts_lock:
        count, first_seen, locked_until = _login_attempts.get(key, (0, now, 0.0))
        if now - first_seen > LOGIN_WINDOW_SECONDS:
            count, first_seen, locked_until = 0, now, 0.0
        count += 1
        if count >= LOGIN_MAX_ATTEMPTS:
            locked_until = now + LOGIN_LOCKOUT_SECONDS
        _login_attempts[key] = (count, first_seen, locked_until)
        # Bound the table so a spray across forged source addresses cannot
        # grow it without limit.
        if len(_login_attempts) > 2048:
            for stale_key, (_c, seen, until) in list(_login_attempts.items()):
                if until < now and now - seen > LOGIN_WINDOW_SECONDS:
                    _login_attempts.pop(stale_key, None)


def _clear_login_failures(key):
    with _login_attempts_lock:
        _login_attempts.pop(key, None)


# ---------------------------------------------------------------------------
# Response hardening
# ---------------------------------------------------------------------------

# The admin UI still loads Bootstrap and Font Awesome from public CDNs, and
# the kiosk embeds the YouTube player, so those origins have to be allowed.
# 'unsafe-inline' for scripts is unavoidable while the page logic lives in
# inline <script> blocks — porting the admin to a bundled frontend is what
# lets this tighten to a nonce.
_CDN_SCRIPTS = 'https://cdn.jsdelivr.net'
_CDN_STYLES = 'https://cdn.jsdelivr.net https://cdnjs.cloudflare.com'
_CSP = (
    "default-src 'self'; "
    "img-src 'self' data: https://img.youtube.com https://i.ytimg.com; "
    "media-src 'self' blob:; "
    f"script-src 'self' 'unsafe-inline' {_CDN_SCRIPTS} https://www.youtube.com https://s.ytimg.com; "
    f"style-src 'self' 'unsafe-inline' {_CDN_STYLES}; "
    "font-src 'self' data: https://cdnjs.cloudflare.com; "
    "connect-src 'self'; "
    "frame-src https://www.youtube.com https://www.youtube-nocookie.com; "
    "frame-ancestors 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


@app.after_request
def security_headers(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('Referrer-Policy', 'same-origin')
    resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    resp.headers.setdefault('Content-Security-Policy', _CSP)
    if app.config['SESSION_COOKIE_SECURE']:
        resp.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    return resp


# ---------------------------------------------------------------------------
# Error handlers — the admin UI is entirely fetch-driven, so an HTML error
# page is indistinguishable from a hang. Always answer API calls with JSON.
# ---------------------------------------------------------------------------

def _error_response(code, message):
    if _wants_json():
        return jsonify({'error': message}), code
    return message, code


@app.errorhandler(400)
def _handle_400(exc):
    return _error_response(400, getattr(exc, 'description', 'Bad request'))


@app.errorhandler(404)
def _handle_404(exc):
    return _error_response(404, getattr(exc, 'description', 'Not found'))


@app.errorhandler(413)
def _handle_413(_exc):
    return _error_response(413, f'File too large. Maximum upload size is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.')


@app.errorhandler(500)
def _handle_500(exc):  # pragma: no cover - exercised only on unexpected faults
    _log.exception('Unhandled error on %s %s: %s', request.method, request.path, exc)
    return _error_response(500, 'Internal server error')


@app.route('/healthz')
def healthz():
    """Liveness probe — checks the database is reachable."""
    try:
        db.session.execute(db.text('SELECT 1'))
        return jsonify({'ok': True, 'agent_version': _server_agent_version()})
    except Exception as exc:  # pylint: disable=broad-except
        _log.error('[health] Database check failed: %s', exc)
        return jsonify({'ok': False, 'error': 'database unavailable'}), 503


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


# Magic-byte signatures for the formats we accept. An extension check alone
# says nothing about what is actually inside the file.
def sniff_image_format(head: bytes):
    """Return 'jpeg'/'png'/'webp' for a recognised header, else None."""
    if head.startswith(b'\xff\xd8\xff'):
        return 'jpeg'
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'webp'
    return None


_HOSTNAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$')


def valid_hostname(value):
    return isinstance(value, str) and bool(_HOSTNAME_RE.match(value))


def client_ip():
    return request.remote_addr or '0.0.0.0'


# ---------------------------------------------------------------------------
# Request body coercion
#
# Every one of these fields used to be read with a bare int()/.strip(), which
# raised on the wrong JSON type and surfaced as an HTML 500 the fetch-based UI
# could not parse. These raise a 400 with the offending field name instead.
# ---------------------------------------------------------------------------

def _bad_request(message) -> typing.NoReturn:
    abort(400, description=message)


def want_str(body, key, max_len=255, required=False):
    value = body.get(key, '')
    if value is None:
        value = ''
    if not isinstance(value, str):
        _bad_request(f"'{key}' must be text")
    value = value.strip()
    if len(value) > max_len:
        _bad_request(f"'{key}' must be at most {max_len} characters")
    if required and not value:
        _bad_request(f"'{key}' is required")
    return value


def want_int(body, key, minimum=None, maximum=None,  # pylint: disable=too-many-positional-arguments
              allow_none=False, clamp=False):
    """Coerce an integer field, rejecting the wrong type with a 400.

    `clamp` keeps the previous silently-clamping behaviour for the fields that
    already had it, so an upgrade does not start rejecting values the admin UI
    has always been allowed to send.
    """
    value = body.get(key)
    if value is None or value == '':
        if allow_none:
            return None
        _bad_request(f"'{key}' is required")
    if isinstance(value, bool):
        _bad_request(f"'{key}' must be a number")
    try:
        value = int(value)
    except (TypeError, ValueError):
        _bad_request(f"'{key}' must be a number")
    if minimum is not None and value < minimum:
        if not clamp:
            _bad_request(f"'{key}' must be at least {minimum}")
        value = minimum
    if maximum is not None and value > maximum:
        if not clamp:
            _bad_request(f"'{key}' must be at most {maximum}")
        value = maximum
    return value


def want_choice(body, key, choices, allow_empty=False):
    value = body.get(key)
    if value in (None, '') and allow_empty:
        return None
    if value not in choices:
        _bad_request(f"'{key}' must be one of: {', '.join(str(c) for c in choices)}")
    return value


def want_bool(body, key):
    value = body.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    if isinstance(value, (int, float)):
        return bool(value)
    _bad_request(f"'{key}' must be true or false")


# ---------------------------------------------------------------------------
# Frame and agent credentials
# ---------------------------------------------------------------------------

def frame_token(frame_id):
    """Stateless per-frame token, derived from the server's secret key.

    Handed to the kiosk at checkin so /next and /signal can tell a registered
    display from anything else that can reach the port. Deriving it rather
    than storing it means no schema change and no per-frame secret to leak
    from the database.
    """
    mac = hmac.new(app.config['SECRET_KEY'].encode('utf-8'),
                   f'frame:{frame_id}'.encode('utf-8'), hashlib.sha256)
    return mac.hexdigest()[:32]


def frame_token_valid(frame_id, token):
    return bool(token) and hmac.compare_digest(str(token), frame_token(frame_id))


def require_frame_auth(frame_id):
    """Enforce the frame token when strict mode is on.

    Off by default: a kiosk still running the previous JavaScript has no token
    to send, and upgrading the server should not blank every display. Turn it
    on from Settings once the frames have reloaded.
    """
    if not get_settings().strict_frame_auth:
        return
    token = request.args.get('t') or request.headers.get('X-Frame-Token')
    if not frame_token_valid(frame_id, token):
        abort(401, description='Invalid or missing frame token')


def agent_credential_ok(frame):
    """True when the request carries this frame's agent bearer token."""
    expected = frame.credential()
    if not expected:
        return False
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return False
    return hmac.compare_digest(auth[7:], expected)


def require_agent_auth(frame):
    """Enforce agent authentication, with a grace period for old agents.

    An agent that predates this release does not send an Authorization header
    on heartbeat. Rather than silently dropping those frames off the
    dashboard, an unauthenticated heartbeat is accepted and logged until
    strict_agent_auth is switched on.
    """
    if agent_credential_ok(frame):
        return
    if get_settings().strict_agent_auth:
        abort(401, description='Invalid or missing agent credential')
    if request.headers.get('Authorization'):
        # A credential was offered and it was wrong — that is never legacy.
        abort(401, description='Invalid agent credential')
    _log.warning('[security] Unauthenticated heartbeat from frame %s (legacy agent)', frame.id)


_DEFAULT_TITLE_ABOVE_OPTIONS = (
    'Now Playing\nComing Soon\nNow in Theaters\n'
    'Get Your Tickets\nFeature Presentation\nNow Showing'
)
_DEFAULT_TITLE_BELOW_OPTIONS = (
    'Now in Theaters\nOnly in Theaters\nReserve Your Seats Today\n'
    'Experience the Magic\nComing Soon to Theaters'
)


def get_settings():
    """Return the singleton Settings row, creating it with defaults if absent.

    Note the retention default applies to *new* databases only. Existing rows
    keep whatever they had — an upgrade must never start deleting history the
    operator did not ask it to delete.
    """
    s = db.session.get(Settings, 1)
    if s:
        return s
    s = Settings(id=1, default_title_above='Now Playing', default_title_below='',
                 default_interval_seconds=300,
                 log_retention_days=30,
                 default_title_above_options=_DEFAULT_TITLE_ABOVE_OPTIONS,
                 default_title_below_options=_DEFAULT_TITLE_BELOW_OPTIONS)
    db.session.add(s)
    try:
        db.session.commit()
    except SAIntegrityError:
        # Another worker created the row between the read and the insert.
        # On a cold start several frames check in at once, so this is a
        # normal outcome rather than an error.
        db.session.rollback()
        s = db.session.get(Settings, 1)
        if s is None:  # pragma: no cover - would mean the insert really failed
            raise
    return s


_agent_version_cache: dict = {}


def _server_agent_version():
    """SHA-256 prefix of the agent source this server would hand out.

    Cached against the file's mtime and size so the hash is not recomputed on
    every heartbeat, but still refreshes when the file is replaced.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'agent', 'agent.py')
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return 'unknown'
    if _agent_version_cache.get('key') == key:
        return _agent_version_cache['value']
    try:
        with open(path, 'rb') as fh:
            value = hashlib.sha256(fh.read()).hexdigest()[:12]
    except OSError:
        return 'unknown'
    _agent_version_cache.update(key=key, value=value)
    return value


# ---------------------------------------------------------------------------
# Video cache — background download worker
# ---------------------------------------------------------------------------

_dl_queue: queue.Queue = queue.Queue()
_dl_queued: set = set()   # youtube_ids in this process's queue
_dl_lock = threading.Lock()

MAX_DOWNLOAD_BYTES = int(os.environ.get('MAX_DOWNLOAD_MB', '600')) * 1024 * 1024
# Refuse to start a download that could leave the volume too full to write the
# database. A full disk takes SQLite down with it.
MIN_FREE_DISK_BYTES = MAX_DOWNLOAD_BYTES * 2 + (256 * 1024 * 1024)
MAX_DOWNLOAD_ATTEMPTS = 5
DOWNLOAD_RETRY_BASE_SECONDS = 300


def _enqueue_download(youtube_id: str) -> None:
    """Add youtube_id to this process's download queue.

    The in-memory set only avoids queueing the same id twice locally; the
    authoritative claim is the conditional UPDATE in _claim_download, which is
    what keeps two gunicorn workers from writing the same file at once.
    """
    if not _YT_DLP_AVAILABLE:
        return
    with _dl_lock:
        if youtube_id in _dl_queued:
            return
        _dl_queued.add(youtube_id)
    _dl_queue.put(youtube_id)


def _claim_download(youtube_id: str) -> bool:
    """Atomically mark a trailer as downloading. False if someone else has it.

    A conditional UPDATE is the whole locking scheme: whichever worker's
    statement affects a row owns the download. Previously each process kept
    its own in-memory set, so two workers could fetch the same video into the
    same path concurrently and corrupt it.
    """
    updated = (db.session.query(Trailer)
               .filter(Trailer.youtube_id == youtube_id,
                       db.or_(Trailer.cache_status.is_(None),
                              Trailer.cache_status.in_(('pending', 'error'))))
               .update({'cache_status': 'downloading',
                        'last_attempt_at': utcnow(),
                        'download_attempts': Trailer.download_attempts + 1},
                       synchronize_session=False))
    db.session.commit()
    return updated > 0


def _finish_download(youtube_id, status, filename=None, error=None):
    trailer = Trailer.query.filter_by(youtube_id=youtube_id).first()
    if not trailer:
        return
    trailer.cache_status = status
    trailer.cached_filename = filename
    trailer.last_error = (error or '')[:255] or None
    db.session.commit()


def _download_ydl_opts(out_path):
    if _FFMPEG_AVAILABLE:
        ydl_fmt = (
            'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]'
            '/bestvideo[height<=720]+bestaudio'
            '/best[height<=720]'
        )
    else:
        # No ffmpeg — select only pre-merged streams (typically ≤480p on YouTube)
        ydl_fmt = 'best[height<=720]/best'
    opts = {
        'format': ydl_fmt,
        'outtmpl': out_path,
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
        'max_filesize': MAX_DOWNLOAD_BYTES,
        'socket_timeout': 30,
        'retries': 2,
    }
    if _FFMPEG_AVAILABLE:
        opts['merge_output_format'] = 'mp4'
    return opts


def _do_download(youtube_id: str) -> None:
    """Download a YouTube video at ≤720p mp4 and update the DB cache status."""
    out_path = os.path.join(VIDEOS_DIR, f'{youtube_id}.mp4')

    with app.app_context():
        if not Trailer.query.filter_by(youtube_id=youtube_id).first():
            return
        if not _claim_download(youtube_id):
            _log.info('[video-cache] %s already claimed elsewhere, skipping', youtube_id)
            return

    try:
        os.makedirs(VIDEOS_DIR, exist_ok=True)
        free = shutil.disk_usage(VIDEOS_DIR).free
        if free < MIN_FREE_DISK_BYTES:
            raise OSError(f'only {free // (1024 * 1024)} MB free, need '
                          f'{MIN_FREE_DISK_BYTES // (1024 * 1024)} MB')

        with _yt_dlp.YoutubeDL(_download_ydl_opts(out_path)) as ydl:
            ydl.download([f'https://www.youtube.com/watch?v={youtube_id}'])

        # yt-dlp honours max_filesize by skipping the download rather than
        # raising, so an oversized video leaves no file behind.
        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise OSError('no output produced (video may exceed the size limit)')

        thumb_path = os.path.join(VIDEOS_DIR, f'{youtube_id}.jpg')
        try:
            r = http_requests.get(
                f'https://img.youtube.com/vi/{youtube_id}/mqdefault.jpg',
                timeout=10,
            )
            if r.status_code == 200:
                with open(thumb_path, 'wb') as _f:
                    _f.write(r.content)
        except Exception:  # pylint: disable=broad-except
            pass

        with app.app_context():
            _finish_download(youtube_id, 'ready', filename=f'{youtube_id}.mp4')
        _log.info('[video-cache] Cached %s', youtube_id)
    except Exception as exc:  # pylint: disable=broad-except
        _log.error('[video-cache] Download failed for %s: %s', youtube_id, exc)
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except OSError:
                pass
        with app.app_context():
            _finish_download(youtube_id, 'error', error=str(exc))


def _download_worker() -> None:
    while True:
        youtube_id = _dl_queue.get()
        try:
            _do_download(youtube_id)
        except Exception as exc:  # pylint: disable=broad-except
            _log.error('[video-cache] Unhandled error for %s: %s', youtube_id, exc)
        finally:
            with _dl_lock:
                _dl_queued.discard(youtube_id)
            _dl_queue.task_done()


def _retry_due(trailer) -> bool:
    """Whether an errored download has waited long enough for another try.

    Exponential backoff from five minutes, capped by attempt count. The old
    behaviour treated 'error' as terminal, so one transient network blip meant
    a trailer never cached again.
    """
    attempts = trailer.download_attempts or 0
    if attempts >= MAX_DOWNLOAD_ATTEMPTS:
        return False
    if not trailer.last_attempt_at:
        return True
    wait = DOWNLOAD_RETRY_BASE_SECONDS * (2 ** max(0, attempts - 1))
    return utcnow() - trailer.last_attempt_at >= timedelta(seconds=min(wait, 21600))


def _maybe_enqueue(trailer) -> None:
    """Queue a trailer for caching if it is eligible right now.

    'pending' and 'downloading' mean someone already owns the work. 'error'
    is retried once its backoff has elapsed — it used to be terminal.
    """
    if trailer.cache_status == 'ready' and trailer.cached_filename:
        return
    if trailer.cache_status in ('pending', 'downloading'):
        return
    if trailer.cache_status == 'error' and not _retry_due(trailer):
        return
    _enqueue_download(trailer.youtube_id)


# ---------------------------------------------------------------------------
# Periodic maintenance
#
# Log pruning used to run inline on every /next request. Moving it here keeps
# the request path to a single insert and lets retention actually apply to
# every frame rather than only the one that happened to ask for content.
# ---------------------------------------------------------------------------

MAINTENANCE_INTERVAL_SECONDS = int(os.environ.get('MAINTENANCE_INTERVAL_SECONDS', '900'))
STALLED_DOWNLOAD_HOURS = 2


def prune_frame_logs():
    """Delete log rows older than the configured retention. No-op when unset."""
    settings = get_settings()
    if not settings.log_retention_days:
        return 0
    cutoff = utcnow() - timedelta(days=settings.log_retention_days)
    removed = FrameLog.query.filter(FrameLog.shown_at < cutoff).delete(synchronize_session=False)
    db.session.commit()
    if removed:
        _log.info('[maintenance] Pruned %d log rows older than %d days',
                  removed, settings.log_retention_days)
    return removed


def sweep_orphan_videos():
    """Delete cached media with no corresponding trailer row."""
    keep = set()
    for (youtube_id,) in db.session.query(Trailer.youtube_id).all():
        keep.add(f'{youtube_id}.mp4')
        keep.add(f'{youtube_id}.jpg')
    removed = 0
    try:
        names = os.listdir(VIDEOS_DIR)
    except OSError:
        return 0
    for name in names:
        if not name.endswith(('.mp4', '.jpg')) or name in keep:
            continue
        try:
            os.remove(os.path.join(VIDEOS_DIR, name))
            removed += 1
        except OSError:
            pass
    if removed:
        _log.info('[maintenance] Removed %d orphaned cache file(s)', removed)
    return removed


def requeue_pending_downloads():
    """Recover stalled downloads and re-queue anything eligible for retry."""
    cutoff = utcnow() - timedelta(hours=STALLED_DOWNLOAD_HOURS)
    stalled = (Trailer.query
               .filter(Trailer.cache_status == 'downloading')
               .filter(db.or_(Trailer.last_attempt_at.is_(None), Trailer.last_attempt_at < cutoff))
               .all())
    for trailer in stalled:
        trailer.cache_status = 'error'
        trailer.last_error = 'download did not complete'
        _log.warning('[maintenance] Reset stalled download for %s', trailer.youtube_id)
    if stalled:
        db.session.commit()

    # A row left 'pending' by a process that died still needs picking up; the
    # request path deliberately leaves those alone, so recover them here.
    for trailer in Trailer.query.filter_by(cache_status='pending').all():
        _enqueue_download(trailer.youtube_id)

    for trailer in Trailer.query.filter_by(active=True).all():
        _maybe_enqueue(trailer)


def run_maintenance():
    prune_frame_logs()
    sweep_orphan_videos()
    requeue_pending_downloads()


def _maintenance_loop() -> None:
    while True:
        time.sleep(MAINTENANCE_INTERVAL_SECONDS)
        try:
            with app.app_context():
                run_maintenance()
        except Exception as exc:  # pylint: disable=broad-except
            _log.error('[maintenance] Sweep failed: %s', exc)


# Threads do not survive fork, so gunicorn --preload would leave the workers
# running in the master and absent from every child. Keying on the pid means
# each process starts its own set exactly once, whenever it first needs them.
_worker_pid = None
_worker_start_lock = threading.Lock()


def start_background_workers():
    global _worker_pid  # pylint: disable=global-statement
    if DISABLE_WORKERS:
        return
    pid = os.getpid()
    with _worker_start_lock:
        if _worker_pid == pid:
            return
        _worker_pid = pid
    threading.Thread(target=_download_worker, daemon=True, name='video-cache-worker').start()
    threading.Thread(target=_maintenance_loop, daemon=True, name='maintenance-worker').start()
    _log.info('[startup] Background workers started in pid %d', pid)


@app.before_request
def _ensure_workers_running():
    if _worker_pid != os.getpid():
        start_background_workers()


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
    ip = client_ip()

    frame = Frame.query.filter_by(ip=ip).first()

    if not frame:
        settings = get_settings()
        # Bypass creates a frame row for any caller that asks. It is a
        # preview convenience, so it is off unless explicitly enabled —
        # otherwise anything that can reach the port can grow the frame
        # table and choose the name the admin panel renders.
        if not (body.get('bypass') and settings.allow_bypass_frames):
            return jsonify({'registered': False})
        raw_hostname = body.get('hostname', '')
        label = raw_hostname if valid_hostname(raw_hostname) else ip
        pinned = settings.default_content_mode == 'pinned'
        frame = Frame(ip=ip, name=f'[Preview] {label}',
                      interval_seconds=settings.default_interval_seconds,
                      rotation=settings.default_rotation,
                      content_mode=settings.default_content_mode,
                      pinned_type=settings.default_pinned_type if pinned else None,
                      pinned_id=settings.default_pinned_id if pinned else None)
        db.session.add(frame)
        db.session.commit()

    frame.last_seen = utcnow()
    db.session.commit()
    return jsonify({
        'registered': True,
        'frame_id': frame.id,
        'frame_token': frame_token(frame.id),
        'interval_seconds': frame.interval_seconds,
        'rotation': frame.rotation,
        'signal_poll_seconds': get_settings().signal_poll_seconds,
    })


def _pick_banner(options_str, fallback):
    """Return a random non-empty line from options_str, or fallback."""
    opts = [o.strip() for o in (options_str or '').splitlines() if o.strip()]
    return random.choice(opts) if opts else fallback


def _select_pool_content(frame, settings, pool_posters, pool_trailers):
    """Pick the next content item from the active pool."""
    pool = pool_posters + pool_trailers
    if not pool:
        return None

    if settings.pool_order == 'sequential':
        last_log = (FrameLog.query.filter_by(frame_id=frame.id)
                    .order_by(FrameLog.shown_at.desc()).first())
        result = pool[0]
        if last_log:
            idx = next(
                (i for i, (ct, it) in enumerate(pool)
                 if ct == last_log.content_type and it.id == last_log.content_id),
                None,
            )
            if idx is not None:
                result = pool[(idx + 1) % len(pool)]
        return result

    # Random mode: exclude last-shown poster to avoid back-to-back repeats.
    last_log = (FrameLog.query.filter_by(frame_id=frame.id)
                .order_by(FrameLog.shown_at.desc()).first())
    last_poster_id = (
        last_log.content_id
        if last_log and last_log.content_type == 'poster'
        else None
    )
    candidates = (
        [p for p in pool_posters if p[1].id != last_poster_id]
        if last_poster_id and len(pool_posters) > 1
        else pool_posters
    )
    weight = settings.trailer_weight_percent
    if weight is None or not candidates or not pool_trailers:
        return random.choice(candidates + pool_trailers)
    if random.randint(0, 99) < weight:
        return random.choice(pool_trailers)
    return random.choice(candidates)


@app.route('/api/frames/<int:frame_id>/next', methods=['GET'])
def frame_next(frame_id):
    frame = db.get_or_404(Frame, frame_id)
    require_frame_auth(frame_id)
    frame.last_seen = utcnow()
    db.session.commit()

    settings = get_settings()
    content = None

    if frame.content_mode == 'pinned' and frame.pinned_type and frame.pinned_id:
        if frame.pinned_type == 'poster':
            item = Poster.query.filter_by(id=frame.pinned_id, active=True).first()
            if item:
                content = ('poster', item)
        elif frame.pinned_type == 'trailer':
            item = Trailer.query.filter_by(id=frame.pinned_id, active=True).first()
            if item:
                content = ('trailer', item)

    if not content:
        pool_posters = [
            ('poster', p)
            for p in Poster.query.filter_by(active=True)
            .order_by(Poster.sort_order, Poster.id).all()
        ]
        pool_trailers = [
            ('trailer', t)
            for t in Trailer.query.filter_by(active=True).order_by(Trailer.id).all()
        ]
        content = _select_pool_content(frame, settings, pool_posters, pool_trailers)

    if not content:
        return jsonify({
            'type': 'empty',
            'rotation': frame.rotation,
            'interval_seconds': frame.interval_seconds,
            'signal_poll_seconds': settings.signal_poll_seconds,
        })

    content_type, item = content
    # A kiosk that hits an unplayable video can fire two fetches within
    # milliseconds; without this guard both land as separate history rows.
    # Retention pruning now runs in the maintenance sweep, not here.
    recent = (FrameLog.query.filter_by(frame_id=frame.id)
              .order_by(FrameLog.shown_at.desc()).first())
    duplicate = (recent is not None
                 and recent.content_type == content_type
                 and recent.content_id == item.id
                 and recent.shown_at is not None
                 and (utcnow() - recent.shown_at) < timedelta(seconds=2))
    if not duplicate:
        db.session.add(FrameLog(frame_id=frame.id, content_type=content_type, content_id=item.id))
        db.session.commit()

    base = {'rotation': frame.rotation, 'interval_seconds': frame.interval_seconds,
            'signal_poll_seconds': settings.signal_poll_seconds}
    if content_type == 'poster':
        title_above = (item.title_above if item.title_above is not None
                       else _pick_banner(settings.default_title_above_options,
                                         settings.default_title_above))
        title_below = (item.title_below if item.title_below is not None
                       else _pick_banner(settings.default_title_below_options,
                                         settings.default_title_below))
        return jsonify({**base, 'type': 'poster', 'id': item.id,
                        'url': f'/images/{item.filename}',
                        'title_above': title_above or '',
                        'title_below': title_below or ''})
    cached_url = (f'/videos/{item.cached_filename}'
                  if item.cache_status == 'ready' and item.cached_filename else None)
    if not cached_url:
        _maybe_enqueue(item)
    return jsonify({**base, 'type': 'trailer', 'id': item.id,
                    'youtube_id': item.youtube_id, 'title': item.title,
                    'cached_url': cached_url})


# ---------------------------------------------------------------------------
# Frame signal / command
# ---------------------------------------------------------------------------

@app.route('/api/frames/<int:frame_id>/signal')
def frame_signal(frame_id):
    """Polled by the frame client every few seconds to receive commands."""
    frame = db.get_or_404(Frame, frame_id)
    require_frame_auth(frame_id)
    cmd = frame.pending_command
    if cmd:
        frame.pending_command = None
        db.session.commit()
    return jsonify({'command': cmd})


@app.route('/api/frames/<int:frame_id>/display-state')
def frame_display_state(frame_id):
    """Whether the display is powered on, for the kiosk's own use.

    The kiosk previously asked the authenticated agent proxy for this and
    always got a 401, so the pause-when-dark behaviour never actually ran.
    This is the same question with no admin session required — it exposes one
    boolean and issues no commands.
    """
    frame = db.get_or_404(Frame, frame_id)
    require_frame_auth(frame_id)
    if not frame.agent_url:
        return jsonify({'on': True, 'agent': False})
    try:
        with closing(_agent_session().get(
            f'{frame.agent_url}/display',
            headers={'Authorization': f'Bearer {frame.credential()}'},
            timeout=(5, 10),
        )) as resp:
            if resp.status_code != 200:
                return jsonify({'on': True, 'agent': False})
            return jsonify({'on': bool(resp.json().get('on', True)), 'agent': True})
    except (http_requests.exceptions.RequestException, ValueError):
        return jsonify({'on': True, 'agent': False})


@app.route('/api/frames/<int:frame_id>/command', methods=['POST'])
def frame_send_command(frame_id):
    frame = db.get_or_404(Frame, frame_id)
    body = request.get_json(silent=True) or {}
    cmd = want_choice(body, 'command', ('next', 'refresh'))
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

    # The extension says nothing about the contents — check the header bytes
    # before this lands in a directory the server hands out over HTTP.
    head = f.stream.read(32)
    f.stream.seek(0)
    if sniff_image_format(head) is None:
        return jsonify({'error': 'That file is not a valid JPEG, PNG, or WebP image.'}), 400

    filename = f'{uuid.uuid4().hex}_{secure_filename(f.filename)}'
    f.save(os.path.join(IMAGES_DIR, filename))

    poster = Poster(
        filename=filename,
        title_above=want_str(request.form, 'title_above') or None,
        title_below=want_str(request.form, 'title_below') or None,
        active=request.form.get('active', 'true').lower() != 'false',
    )
    db.session.add(poster)
    db.session.commit()
    return jsonify(poster.to_dict()), 201


@app.route('/api/posters/reorder', methods=['POST'])
def reorder_posters():
    items = request.get_json(silent=True) or []
    if not isinstance(items, list):
        return jsonify({'error': 'Expected a list of {id, sort_order} objects'}), 400
    for item in items:
        if not isinstance(item, dict):
            return jsonify({'error': 'Expected a list of {id, sort_order} objects'}), 400
        poster = db.session.get(Poster, item.get('id'))
        if poster is not None:
            poster.sort_order = want_int(item, 'sort_order', minimum=-100000, maximum=100000)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/posters/<int:poster_id>', methods=['PATCH'])
def update_poster(poster_id):
    poster = db.get_or_404(Poster, poster_id)
    body = request.get_json(silent=True) or {}
    if 'title_above' in body:
        poster.title_above = want_str(body, 'title_above') or None
    if 'title_below' in body:
        poster.title_below = want_str(body, 'title_below') or None
    if 'active' in body:
        poster.active = want_bool(body, 'active')
    if 'sort_order' in body:
        poster.sort_order = want_int(body, 'sort_order', minimum=-100000, maximum=100000)
    db.session.commit()
    return jsonify(poster.to_dict())


@app.route('/api/posters/<int:poster_id>', methods=['DELETE'])
def delete_poster(poster_id):
    poster = db.get_or_404(Poster, poster_id)
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


@app.route('/videos/<path:filename>')
def serve_video(filename):
    return send_from_directory(VIDEOS_DIR, filename)


@app.route('/api/trailers', methods=['POST'])
def add_trailer():
    body = request.get_json(silent=True) or {}
    raw_url = want_str(body, 'url', max_len=2048, required=True)
    title = want_str(body, 'title', required=True)

    youtube_id = parse_youtube_id(raw_url)
    if not youtube_id:
        return jsonify({'error': 'Could not parse a YouTube video ID from the provided URL'}), 400

    existing = Trailer.query.filter_by(youtube_id=youtube_id).first()
    if existing:
        return jsonify(existing.to_dict()), 200

    trailer = Trailer(youtube_id=youtube_id, title=title)
    db.session.add(trailer)
    db.session.commit()
    _enqueue_download(youtube_id)
    return jsonify(trailer.to_dict()), 201


@app.route('/api/trailers/<int:trailer_id>', methods=['PATCH'])
def update_trailer(trailer_id):
    trailer = db.get_or_404(Trailer, trailer_id)
    body = request.get_json(silent=True) or {}
    if 'title' in body:
        trailer.title = want_str(body, 'title', required=True)
    if 'active' in body:
        trailer.active = want_bool(body, 'active')
    db.session.commit()
    return jsonify(trailer.to_dict())


@app.route('/api/trailers/<int:trailer_id>', methods=['DELETE'])
def delete_trailer(trailer_id):
    trailer = db.get_or_404(Trailer, trailer_id)
    _remove_cached_files(trailer.youtube_id)
    db.session.delete(trailer)
    db.session.commit()
    return '', 204


@app.route('/api/trailers/<int:trailer_id>/cache', methods=['DELETE'])
def clear_trailer_cache(trailer_id):
    """Remove the cached video and thumbnail, reset status, re-enqueue download."""
    trailer = db.get_or_404(Trailer, trailer_id)
    _remove_cached_files(trailer.youtube_id)
    trailer.cache_status = None
    trailer.cached_filename = None
    trailer.download_attempts = 0
    trailer.last_attempt_at = None
    trailer.last_error = None
    db.session.commit()
    _enqueue_download(trailer.youtube_id)
    return jsonify(trailer.to_dict())


def _remove_cached_files(youtube_id: str) -> None:
    """Remove the cached mp4 and thumbnail for a given youtube_id."""
    for name in (f'{youtube_id}.mp4', f'{youtube_id}.jpg'):
        try:
            os.remove(os.path.join(VIDEOS_DIR, name))
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# Frame admin API
# ---------------------------------------------------------------------------

def _resolve_content(logs):
    """Map log rows to display previews using two batched lookups.

    Previously this ran a poster/trailer SELECT per log row, on an endpoint
    the dashboard polls every 30 seconds.
    """
    poster_ids = {l.content_id for l in logs if l.content_type == 'poster'}
    trailer_ids = {l.content_id for l in logs if l.content_type == 'trailer'}
    posters = ({p.id: p for p in Poster.query.filter(Poster.id.in_(poster_ids)).all()}
               if poster_ids else {})
    trailers = ({t.id: t for t in Trailer.query.filter(Trailer.id.in_(trailer_ids)).all()}
                if trailer_ids else {})

    previews = {}
    for log in logs:
        shown_at = log.shown_at.isoformat() if log.shown_at else None
        if log.content_type == 'poster':
            item = posters.get(log.content_id)
            previews[log.id] = None if item is None else {
                'type': 'poster',
                'shown_at': shown_at,
                'thumb_url': f'/images/{item.filename}',
                'title': item.title_above or item.title_below or item.filename,
            }
        else:
            item = trailers.get(log.content_id)
            previews[log.id] = None if item is None else {
                'type': 'trailer',
                'shown_at': shown_at,
                'thumb_url': item.to_dict()['thumb_url'],
                'title': item.title,
            }
    return previews


def _latest_logs_for(frame_ids):
    """Most recent log row per frame, in one grouped query plus one fetch."""
    if not frame_ids:
        return []
    newest = (db.session.query(db.func.max(FrameLog.id))
              .filter(FrameLog.frame_id.in_(frame_ids))
              .group_by(FrameLog.frame_id)
              .all())
    ids = [row[0] for row in newest if row[0] is not None]
    if not ids:
        return []
    return FrameLog.query.filter(FrameLog.id.in_(ids)).all()


@app.route('/api/frames', methods=['GET'])
def get_frames():
    frames = Frame.query.order_by(Frame.name).all()
    logs = _latest_logs_for([f.id for f in frames])
    previews = _resolve_content(logs)
    by_frame = {l.frame_id: previews.get(l.id) for l in logs}

    result = []
    for f in frames:
        d = f.to_dict()
        d['preview'] = by_frame.get(f.id)
        result.append(d)
    return jsonify(result)


@app.route('/api/frames/<int:frame_id>', methods=['GET'])
def get_frame(frame_id):
    return jsonify(db.get_or_404(Frame, frame_id).to_dict())


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
    frame = db.get_or_404(Frame, frame_id)
    FrameLog.query.filter_by(frame_id=frame.id).delete()
    RegistrationToken.query.filter_by(frame_id=frame.id).update({'frame_id': None})
    db.session.delete(frame)
    db.session.commit()
    return '', 204


@app.route('/api/frames/<int:frame_id>', methods=['PATCH'])
def update_frame(frame_id):
    frame = db.get_or_404(Frame, frame_id)
    body = request.get_json(silent=True) or {}
    if 'name' in body:
        frame.name = want_str(body, 'name') or None
    if 'rotation' in body:
        frame.rotation = want_choice(body, 'rotation', (0, 90, 180, 270))
    if 'interval_seconds' in body:
        frame.interval_seconds = want_int(body, 'interval_seconds', minimum=10, maximum=86400, clamp=True)
    if 'content_mode' in body:
        frame.content_mode = want_choice(body, 'content_mode', ('pool', 'pinned'))
    if 'pinned_type' in body:
        frame.pinned_type = want_choice(body, 'pinned_type', ('poster', 'trailer'), allow_empty=True)
    if 'pinned_id' in body:
        frame.pinned_id = want_int(body, 'pinned_id', minimum=1, allow_none=True)
    db.session.commit()
    return jsonify(frame.to_dict())


# ---------------------------------------------------------------------------
# Dashboard summary API
# ---------------------------------------------------------------------------

@app.route('/api/frames/apply-defaults', methods=['POST'])
def apply_defaults_to_frames():
    s = get_settings()
    frames = Frame.query.all()
    for frame in frames:
        frame.interval_seconds = s.default_interval_seconds
        frame.rotation         = s.default_rotation
        frame.content_mode     = s.default_content_mode
        frame.pinned_type      = s.default_pinned_type
        frame.pinned_id        = s.default_pinned_id
    db.session.commit()
    return jsonify({'updated': len(frames)})


@app.route('/api/dashboard/summary', methods=['GET'])
def dashboard_summary():
    logs = (FrameLog.query
            .order_by(FrameLog.shown_at.desc())
            .limit(10)
            .all())
    previews = _resolve_content(logs)
    frame_names = {}
    frame_ids = {l.frame_id for l in logs}
    if frame_ids:
        for f in Frame.query.filter(Frame.id.in_(frame_ids)).all():
            frame_names[f.id] = f.name or f.ip

    activity = []
    for log in logs:
        preview = previews.get(log.id) or {}
        activity.append({
            'frame_name': frame_names.get(log.frame_id, 'Unknown'),
            'content_type': log.content_type,
            'shown_at': log.shown_at.isoformat() if log.shown_at else None,
            'title': preview.get('title'),
            'thumb_url': preview.get('thumb_url'),
        })

    return jsonify({
        'posters_active': Poster.query.filter_by(active=True).count(),
        'posters_total': Poster.query.count(),
        'trailers_active': Trailer.query.filter_by(active=True).count(),
        'trailers_total': Trailer.query.count(),
        'trailers_ready': Trailer.query.filter_by(cache_status='ready').count(),
        'trailers_error': Trailer.query.filter_by(cache_status='error').count(),
        'tokens_unused': RegistrationToken.query.filter_by(used_at=None).count(),
        'activity': activity,
    })


# ---------------------------------------------------------------------------
# Agent registration + proxy
# ---------------------------------------------------------------------------

def _install_command(base_url, token_value):
    """Two-step installer command.

    Piping the response straight into `sudo bash` means the machine executes
    whatever answered the request, with no chance to look first. Downloading
    to a file and running it separately at least makes the script inspectable
    and keeps a copy of what actually ran.
    """
    return (f"curl -fsSL {base_url}/install.sh -o frameit-install.sh && "
            f"sudo bash frameit-install.sh --server {base_url} --token {token_value}")


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
            'install_cmd': _install_command(base_url, t.token),
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
    token = db.get_or_404(RegistrationToken, token_id)
    if token.used_at:
        return jsonify({'error': 'Cannot revoke a token that has already been used'}), 400
    db.session.delete(token)
    db.session.commit()
    return '', 204


def _claim_frame_ip(frame, ip):
    """Point a frame row at `ip`, clearing a stale row that already holds it.

    Frame.ip is unique, so a Pi that picks up a new DHCP lease used to be
    unable to re-register at all. Any row occupying the address that has never
    had an agent is a leftover preview and gets removed.
    """
    if frame.ip == ip:
        return True
    conflict = Frame.query.filter(Frame.ip == ip, Frame.id != frame.id).first()
    if conflict is not None:
        if conflict.agent_url:
            return False
        FrameLog.query.filter_by(frame_id=conflict.id).delete()
        RegistrationToken.query.filter_by(frame_id=conflict.id).update({'frame_id': None})
        db.session.delete(conflict)
        db.session.flush()
    frame.ip = ip
    return True


@app.route('/api/agents/register', methods=['POST'])
def agent_register():
    body = request.get_json(silent=True) or {}
    token_value = want_str(body, 'token', max_len=128)
    hostname = body.get('hostname', '')
    # Anything here lands in the agent URL's authority, so a value like
    # "5001@attacker.example" would redirect every proxied request — bearer
    # token included — at a host of the caller's choosing.
    port = want_int(body, 'port', minimum=1, maximum=65535) if 'port' in body else 5001
    wants_secret = bool(body.get('supports_secret'))

    if not valid_hostname(hostname):
        hostname = ''

    token = RegistrationToken.query.filter_by(token=token_value).first()
    if not token:
        return jsonify({'error': 'Invalid token'}), 401

    ip = client_ip()
    agent_url = f'http://{ip}:{port}'

    if token.used_at:
        # Re-registration after an agent restart or update. Possession of the
        # token is the proof — the previous check compared remote_addr, which
        # is forgeable when the server is not behind a trusted proxy and also
        # locked out any frame whose DHCP lease had changed.
        if not token.frame_id:
            return jsonify({'error': 'Token already used'}), 401
        frame = db.session.get(Frame, token.frame_id)
        if not frame:
            return jsonify({'error': 'Token already used'}), 401
        if not _claim_frame_ip(frame, ip):
            return jsonify({'error': 'Another frame is already registered at this address'}), 409
    else:
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
        frame.agent_token = token_value
        token.used_at = utcnow()

    frame.agent_url = agent_url
    frame.agent_last_seen = utcnow()
    if not frame.name:
        frame.name = hostname

    # Issue a credential that is independent of the one-time registration
    # token. Agents that predate this release do not ask for one and keep
    # using the token, so an upgrade does not strand them.
    response = {'ok': True}
    if wants_secret:
        frame.agent_secret = secrets.token_hex(32)
        response['agent_secret'] = frame.agent_secret

    db.session.flush()
    token.frame_id = frame.id
    db.session.commit()
    response['frame_id'] = frame.id
    return jsonify(response)


@app.route('/api/agents/<int:frame_id>/heartbeat', methods=['POST'])
def agent_heartbeat(frame_id):
    frame = db.get_or_404(Frame, frame_id)
    require_agent_auth(frame)
    frame.agent_last_seen = utcnow()
    body = request.get_json(silent=True) or {}
    if 'version' in body:
        frame.agent_version = want_str(body, 'version', max_len=12) or None
    db.session.commit()
    return jsonify({'interval_seconds': frame.interval_seconds, 'rotation': frame.rotation})


@app.route('/api/agent/version')
def agent_server_version():
    return jsonify({'version': _server_agent_version()})


# One pooled session for all agent traffic. Each proxied call previously
# opened a fresh TCP connection.
_agent_http = threading.local()


def _agent_session():
    session_obj = getattr(_agent_http, 'session', None)
    if session_obj is None:
        session_obj = http_requests.Session()
        _agent_http.session = session_obj
    return session_obj


# Agent endpoints the proxy is willing to reach. The subpath used to be
# forwarded verbatim, so the proxy would relay anything the caller named.
_AGENT_SUBPATHS = {
    'health', 'system/info', 'system/reboot', 'system/agent-update',
    'system/update', 'system/upgrade', 'system/services',
    'network/status', 'network/wifi/scan', 'network/wifi/connect',
    'display', 'display/on', 'display/off',
}
_AGENT_SUBPATH_RE = re.compile(r'^system/services/[A-Za-z0-9_.-]{1,64}/restart$')


def _agent_subpath_allowed(subpath):
    return subpath in _AGENT_SUBPATHS or bool(_AGENT_SUBPATH_RE.match(subpath))


@app.route('/api/frames/<int:frame_id>/agent/<path:subpath>', methods=['GET', 'POST', 'PATCH', 'DELETE'])
def agent_proxy(frame_id, subpath):
    frame = db.get_or_404(Frame, frame_id)
    if not frame.agent_url:
        return jsonify({'error': 'No agent registered for this frame'}), 404
    if not _agent_subpath_allowed(subpath):
        return jsonify({'error': f'Unknown agent endpoint: {subpath}'}), 404

    target = f"{frame.agent_url}/{subpath}"
    headers = {'Authorization': f'Bearer {frame.credential()}', 'Content-Type': 'application/json'}

    try:
        resp = _agent_session().request(
            method=request.method,
            url=target,
            headers=headers,
            json=request.get_json(silent=True),
            stream=True,
            timeout=(10, 300),  # 10s connect, 5min read between chunks
        )
    except (http_requests.exceptions.ConnectionError, http_requests.exceptions.Timeout):
        return jsonify({'error': 'Agent unreachable'}), 503
    except http_requests.exceptions.RequestException as exc:
        _log.error('[agent-proxy] %s failed: %s', target, exc)
        return jsonify({'error': 'Agent request failed'}), 502

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
    #
    # The close() in the finally clause is what keeps an abandoned stream —
    # an admin closing the modal mid-upgrade — from leaking the connection.
    def _relay():
        try:
            if ct.startswith('text/plain'):
                for line in resp.iter_lines(decode_unicode=True):
                    yield line + '\n'
            else:
                yield from resp.iter_content(chunk_size=512)
        finally:
            resp.close()

    return Response(stream_with_context(_relay()), status=resp.status_code,
                    content_type=ct, headers=fwd_headers)


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
def update_settings():  # pylint: disable=too-many-branches
    s = get_settings()
    body = request.get_json(silent=True) or {}
    if 'default_title_above' in body:
        s.default_title_above = want_str(body, 'default_title_above') or None
    if 'default_title_below' in body:
        s.default_title_below = want_str(body, 'default_title_below') or None
    if 'default_interval_seconds' in body:
        s.default_interval_seconds = want_int(body, 'default_interval_seconds',
                                              minimum=10, maximum=86400, clamp=True)
    if 'default_rotation' in body:
        s.default_rotation = want_choice(body, 'default_rotation', (0, 90, 180, 270))
    if 'default_content_mode' in body:
        s.default_content_mode = want_choice(body, 'default_content_mode', ('pool', 'pinned'))
    if 'default_pinned_type' in body:
        s.default_pinned_type = want_choice(body, 'default_pinned_type',
                                            ('poster', 'trailer'), allow_empty=True)
    if 'default_pinned_id' in body:
        s.default_pinned_id = want_int(body, 'default_pinned_id', minimum=1, allow_none=True)
    if 'pool_order' in body:
        s.pool_order = want_choice(body, 'pool_order', ('random', 'sequential'))
    if 'trailer_weight_percent' in body:
        s.trailer_weight_percent = want_int(body, 'trailer_weight_percent',
                                            minimum=0, maximum=100, allow_none=True)
    if 'dashboard_refresh_seconds' in body:
        s.dashboard_refresh_seconds = want_int(body, 'dashboard_refresh_seconds',
                                               minimum=5, maximum=3600, clamp=True)
    if 'signal_poll_seconds' in body:
        s.signal_poll_seconds = want_int(body, 'signal_poll_seconds',
                                         minimum=1, maximum=60, clamp=True)
    if 'log_retention_days' in body:
        s.log_retention_days = want_int(body, 'log_retention_days',
                                        minimum=1, maximum=3650, allow_none=True, clamp=True)
    if 'default_title_above_options' in body:
        s.default_title_above_options = want_str(body, 'default_title_above_options',
                                                 max_len=4000) or None
    if 'default_title_below_options' in body:
        s.default_title_below_options = want_str(body, 'default_title_below_options',
                                                 max_len=4000) or None
    if 'strict_agent_auth' in body:
        s.strict_agent_auth = want_bool(body, 'strict_agent_auth')
    if 'strict_frame_auth' in body:
        s.strict_frame_auth = want_bool(body, 'strict_frame_auth')
    if 'allow_bypass_frames' in body:
        s.allow_bypass_frames = want_bool(body, 'allow_bypass_frames')
    db.session.commit()
    return jsonify(s.to_dict())


@app.route('/api/settings/agent-auth-readiness', methods=['GET'])
def agent_auth_readiness():
    """Report whether every registered agent has moved to its own credential.

    Answers the only question that matters before turning strict mode on:
    which frames would stop reporting in.
    """
    frames = Frame.query.filter(Frame.agent_url.isnot(None)).all()
    legacy = [{'id': f.id, 'name': f.name or f.ip} for f in frames if not f.agent_secret]
    return jsonify({
        'total_agents': len(frames),
        'legacy_agents': legacy,
        'ready': not legacy,
    })


# ---------------------------------------------------------------------------
# Admin auth routes
# ---------------------------------------------------------------------------

def _sign_in(user):
    """Establish an authenticated session bound to the current password."""
    session.clear()
    session['admin_user'] = user.username
    session['admin_fp'] = _password_fingerprint(user.password_hash)
    session.permanent = True


def _safe_next(target):
    """Only follow a same-site relative path — never an absolute URL."""
    if not target or not target.startswith('/') or target.startswith('//'):
        return url_for('admin_index')
    return target


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
        elif len(password) < MIN_PASSWORD_LENGTH:
            error = f'Password must be at least {MIN_PASSWORD_LENGTH} characters.'
        else:
            user = AdminUser(username=username, password_hash=generate_password_hash(password))
            db.session.add(user)
            db.session.commit()
            _sign_in(user)
            return redirect(url_for('admin_index'))
    return render_template('admin_setup.html', error=error)


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if _current_admin() is not None:
        return redirect(url_for('admin_index'))
    error = None
    if request.method == 'POST':
        throttle_key = client_ip()
        blocked_for = _login_blocked(throttle_key)
        if blocked_for:
            error = f'Too many failed attempts. Try again in {blocked_for // 60 + 1} minute(s).'
            return render_template('admin_login.html', error=error), 429
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = AdminUser.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            _clear_login_failures(throttle_key)
            _sign_in(user)
            return redirect(_safe_next(request.args.get('next')))
        _record_login_failure(throttle_key)
        _log.warning('[security] Failed login for %r from %s', username[:64], throttle_key)
        error = 'Invalid username or password.'
    return render_template('admin_login.html', error=error)


@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))


@app.route('/admin/password', methods=['POST'])
def admin_change_password():
    user = _current_admin()
    if user is None:
        return jsonify({'error': 'Unauthorized'}), 401
    body = request.get_json(silent=True) or {}
    current = body.get('current', '')
    new_pass = body.get('new', '')
    if not isinstance(current, str) or not isinstance(new_pass, str):
        return jsonify({'error': 'Passwords must be text.'}), 400
    if not check_password_hash(user.password_hash, current):
        return jsonify({'error': 'Current password is incorrect.'}), 400
    if len(new_pass) < MIN_PASSWORD_LENGTH:
        return jsonify({'error': f'New password must be at least {MIN_PASSWORD_LENGTH} characters.'}), 400
    user.password_hash = generate_password_hash(new_pass)
    db.session.commit()
    # Re-bind this session to the new hash; every other session's fingerprint
    # no longer matches, so they are signed out on their next request.
    _sign_in(user)
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
    ('trailer',  'cache_status',               'VARCHAR(12)'),
    ('trailer',  'cached_filename',            'VARCHAR(24)'),
    ('settings', 'pool_order',                 "VARCHAR(10) NOT NULL DEFAULT 'random'"),
    ('settings', 'trailer_weight_percent',     'INTEGER'),
    ('settings', 'dashboard_refresh_seconds',  'INTEGER NOT NULL DEFAULT 30'),
    ('settings', 'signal_poll_seconds',        'INTEGER NOT NULL DEFAULT 1'),
    ('settings', 'log_retention_days',         'INTEGER'),
    ('settings', 'default_title_above_options','TEXT'),
    ('settings', 'default_title_below_options','TEXT'),
    ('frame',    'agent_secret',               'VARCHAR(64)'),
    ('trailer',  'download_attempts',          'INTEGER NOT NULL DEFAULT 0'),
    ('trailer',  'last_attempt_at',            'DATETIME'),
    ('trailer',  'last_error',                 'VARCHAR(255)'),
    ('settings', 'strict_agent_auth',          'BOOLEAN NOT NULL DEFAULT 0'),
    ('settings', 'strict_frame_auth',          'BOOLEAN NOT NULL DEFAULT 0'),
    ('settings', 'allow_bypass_frames',        'BOOLEAN NOT NULL DEFAULT 0'),
]

# Indexes created after the fact on existing databases. db.create_all() only
# builds these for tables it creates, so an upgraded database needs them added
# explicitly. CREATE INDEX IF NOT EXISTS is idempotent, so this is safe to
# re-run on every startup and safe to race between workers.
#
# Table names come from the models rather than string literals — spelling one
# wrong here fails silently, which is exactly how the framelog index went
# missing on the upgrade path while passing on a fresh database.
_INDEXES = [
    ('ix_framelog_frame_shown', FrameLog.__tablename__, '(frame_id, shown_at)'),
    ('ix_framelog_shown',       FrameLog.__tablename__, '(shown_at)'),
    ('ix_trailer_cache_status', Trailer.__tablename__,  '(cache_status)'),
]


def _table_exists(conn, table):
    row = conn.execute(
        db.text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"),
        {'t': table},
    ).fetchone()
    return row is not None


def migrate_schema():
    """Apply pending column and index additions — safe to call on every startup.

    Every step is idempotent and tolerates losing a race to another gunicorn
    worker, because all of them run this at once on a cold start.
    """
    added = set()
    with db.engine.connect() as conn:
        for table, column, definition in _MIGRATIONS:
            if not _table_exists(conn, table):
                continue
            rows = conn.execute(db.text(f'PRAGMA table_info("{table}")')).fetchall()
            existing = {r[1] for r in rows}
            if column not in existing:
                try:
                    conn.execute(db.text(f'ALTER TABLE "{table}" ADD COLUMN {column} {definition}'))
                    conn.commit()
                    added.add((table, column))
                    _log.info('[db] Added column %s.%s', table, column)
                except SAOperationalError as exc:
                    if 'duplicate column name' not in str(exc).lower():
                        raise
                    conn.rollback()  # another worker won the race — column already exists

        # Seed banner text options the first time these columns are added to an
        # existing DB — so current users get the defaults without needing a
        # manual settings save.
        _seeds = {
            'default_title_above_options': _DEFAULT_TITLE_ABOVE_OPTIONS,
            'default_title_below_options': _DEFAULT_TITLE_BELOW_OPTIONS,
        }
        for col, val in _seeds.items():
            if ('settings', col) in added:
                conn.execute(
                    db.text(f'UPDATE settings SET {col} = :v WHERE id = 1'),
                    {'v': val},
                )
                conn.commit()

        for name, table, columns in _INDEXES:
            if not _table_exists(conn, table):
                # Never silent: a missing table here means a typo or a schema
                # that failed to create, not a condition to shrug off.
                _log.warning('[db] Skipping index %s — table %r does not exist', name, table)
                continue
            try:
                conn.execute(db.text(f'CREATE INDEX IF NOT EXISTS {name} ON "{table}" {columns}'))
                conn.commit()
            except SAOperationalError as exc:  # pragma: no cover - lost race
                _log.warning('[db] Index %s not created: %s', name, exc)
                conn.rollback()


def init_database():
    """Create missing tables, migrate existing ones, seed the settings row."""
    db.create_all()
    migrate_schema()
    # Materialise the singleton now, while startup is single-threaded, rather
    # than letting the first concurrent burst of frame check-ins race to
    # create it.
    get_settings()


@app.cli.command('init-db')
def init_db_command():
    init_database()
    print('Database initialized.')


@app.cli.command('maintenance')
def maintenance_command():
    """Run the housekeeping sweep by hand (prune logs, sweep orphans, retry downloads)."""
    run_maintenance()
    print('Maintenance sweep complete.')


with app.app_context():
    init_database()

start_background_workers()

if __name__ == '__main__':
    # Binding the debug server to every interface exposes the Werkzeug
    # console, which is unauthenticated remote code execution. Both are now
    # opt-in.
    app.run(
        debug=os.environ.get('FLASK_DEBUG', '').lower() in ('1', 'true', 'yes'),
        host=os.environ.get('FLASK_RUN_HOST', '127.0.0.1'),
        port=int(os.environ.get('FLASK_RUN_PORT', '5000')),
    )
