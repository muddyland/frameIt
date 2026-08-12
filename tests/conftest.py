"""
Shared fixtures for the FrameIT main app test suite.

The DATA_DIR and IMAGES_DIR env vars must be set *before* main.py is
imported (because main.py runs db.create_all() at module level).  Setting
them here at module scope — before any test import — achieves that.
"""
import io
import os
import secrets
import tempfile

import pytest
from flask.testing import FlaskClient

# ── Point the app at throwaway directories before importing ────────────────
_tmp_data   = tempfile.mkdtemp(prefix='frameit_test_data_')
_tmp_images = tempfile.mkdtemp(prefix='frameit_test_images_')
_tmp_videos = tempfile.mkdtemp(prefix='frameit_test_videos_')
os.environ['DATA_DIR']   = _tmp_data
os.environ['IMAGES_DIR'] = _tmp_images
os.environ['VIDEOS_DIR'] = _tmp_videos
# Keep the download worker and maintenance sweep out of the test process —
# otherwise they mutate cache_status underneath assertions.
os.environ['FRAMEIT_DISABLE_WORKERS'] = '1'

from main import app as flask_app   # noqa: E402  (import after env setup)
from models import db, Settings     # noqa: E402


# ── Minimal but structurally valid image payloads ──────────────────────────
# The upload endpoint sniffs magic bytes, so a placeholder string is no
# longer accepted. These are the smallest headers each decoder recognises.
JPEG_BYTES = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00' + b'\x00' * 32
PNG_BYTES = b'\x89PNG\r\n\x1a\n' + b'\x00' * 40
WEBP_BYTES = b'RIFF\x24\x00\x00\x00WEBPVP8 ' + b'\x00' * 32

IMAGE_BYTES = {'jpg': JPEG_BYTES, 'jpeg': JPEG_BYTES, 'png': PNG_BYTES, 'webp': WEBP_BYTES}


def image_bytes_for(filename):
    return IMAGE_BYTES.get(filename.rsplit('.', 1)[-1].lower(), JPEG_BYTES)


class CSRFClient(FlaskClient):
    """Test client that carries this session's CSRF token on every request.

    Mirrors what the admin UI does — the token is read from the session
    rather than hard-coded, so a broken CSRF implementation still fails the
    tests rather than being papered over.
    """

    def open(self, *args, **kwargs):
        headers = dict(kwargs.pop('headers', None) or {})
        headers.setdefault('X-CSRF-Token', self.csrf_token())
        kwargs['headers'] = headers
        return super().open(*args, **kwargs)

    def csrf_token(self):
        with self.session_transaction() as sess:
            token = sess.get('_csrf')
            if not token:
                token = secrets.token_urlsafe(32)
                sess['_csrf'] = token
            return token


flask_app.test_client_class = CSRFClient


# ── App / DB fixtures ──────────────────────────────────────────────────────

@pytest.fixture(scope='session')
def app():
    """Single Flask app instance for the whole test session."""
    flask_app.config['TESTING'] = True
    with flask_app.app_context():
        db.create_all()
    yield flask_app


@pytest.fixture(autouse=True)
def clean_tables(app):          # noqa: redefined-outer-name
    """Wipe all rows between tests without recreating the schema."""
    yield
    with app.app_context():
        db.session.remove()
        for table in reversed(db.metadata.sorted_tables):
            db.session.execute(table.delete())
        db.session.commit()


@pytest.fixture
def client(app):                # noqa: redefined-outer-name
    """Authenticated test client — creates an admin user and logs in."""
    c = app.test_client()
    # /admin/setup is public and creates the first admin user + sets session
    c.post('/admin/setup', data={
        'username': 'admin',
        'password': 'testpass',
        'confirm':  'testpass',
    })
    return c


@pytest.fixture
def raw_client(app):            # noqa: redefined-outer-name
    """Unauthenticated client that does NOT attach a CSRF token."""
    return FlaskClient(app, app.response_class, use_cookies=True)


@pytest.fixture
def allow_bypass(app, client):  # noqa: redefined-outer-name
    """Enable bypass frame creation, which is off by default."""
    with app.app_context():
        settings = db.session.get(Settings, 1)
        if settings is None:
            settings = Settings(id=1)
            db.session.add(settings)
        settings.allow_bypass_frames = True
        db.session.commit()
    return client


# ── Helpers ────────────────────────────────────────────────────────────────

def make_image_upload(filename='poster.jpg', content=None):
    """Return a dict suitable for client.post(data=..., content_type='multipart/form-data')."""
    return {
        'file': (io.BytesIO(content if content is not None else image_bytes_for(filename)),
                 filename),
    }


def upload_poster(client, filename='poster.jpg', title_above='Now Playing',
                  title_below='In Theaters', active='true'):
    """Helper: POST a fake poster and return the response JSON."""
    data = {
        'file': (io.BytesIO(image_bytes_for(filename)), filename),
        'title_above': title_above,
        'title_below': title_below,
        'active': active,
    }
    resp = client.post('/api/posters/upload', data=data,
                       content_type='multipart/form-data')
    return resp


def add_trailer(client, url='dQw4w9WgXcQ', title='Test Trailer'):
    """Helper: POST a trailer and return the response."""
    return client.post('/api/trailers',
                       json={'url': url, 'title': title},
                       content_type='application/json')


def enable_bypass():
    """Turn on bypass frame creation inside an active app context."""
    settings = db.session.get(Settings, 1)
    if settings is None:
        settings = Settings(id=1)
        db.session.add(settings)
    settings.allow_bypass_frames = True
    db.session.commit()


def checkin(client, hostname='testframe'):
    """Helper: checkin a frame and return the parsed JSON.

    Uses bypass=True so the frame is auto-created without a registered agent,
    matching the behaviour of the ?bypass_install=1 preview path. Bypass is
    disabled by default, so it is enabled here first.
    """
    with client.application.app_context():
        enable_bypass()
    resp = client.post('/api/frames/checkin',
                       json={'hostname': hostname, 'bypass': True},
                       content_type='application/json')
    return resp.get_json()
