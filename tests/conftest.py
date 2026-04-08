"""
Shared fixtures for the FrameIT main app test suite.

The DATA_DIR and IMAGES_DIR env vars must be set *before* main.py is
imported (because main.py runs db.create_all() at module level).  Setting
them here at module scope — before any test import — achieves that.
"""
import io
import os
import tempfile

import pytest

# ── Point the app at throwaway directories before importing ────────────────
_tmp_data   = tempfile.mkdtemp(prefix='frameit_test_data_')
_tmp_images = tempfile.mkdtemp(prefix='frameit_test_images_')
os.environ['DATA_DIR']   = _tmp_data
os.environ['IMAGES_DIR'] = _tmp_images

from main import app as flask_app   # noqa: E402  (import after env setup)
from models import db               # noqa: E402


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


# ── Helpers ────────────────────────────────────────────────────────────────

def make_image_upload(filename='poster.jpg', content=b'FAKEJPEG'):
    """Return a dict suitable for client.post(data=..., content_type='multipart/form-data')."""
    return {
        'file': (io.BytesIO(content), filename),
    }


def upload_poster(client, filename='poster.jpg', title_above='Now Playing',
                  title_below='In Theaters', active='true'):
    """Helper: POST a fake poster and return the response JSON."""
    data = {
        'file': (io.BytesIO(b'FAKEJPEG'), filename),
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


def checkin(client, hostname='testframe'):
    """Helper: checkin a frame and return the parsed JSON.

    Uses bypass=True so the frame is auto-created without a registered agent,
    matching the behaviour of the ?bypass_install=1 preview path.
    """
    resp = client.post('/api/frames/checkin',
                       json={'hostname': hostname, 'bypass': True},
                       content_type='application/json')
    return resp.get_json()
