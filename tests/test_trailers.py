"""Tests for the Trailer CRUD API and YouTube ID parsing."""
import os
from unittest.mock import patch

import pytest

import main as main_module
from main import parse_youtube_id, VIDEOS_DIR
from models import db, Trailer
from tests.conftest import add_trailer, checkin


class TestParseYoutubeId:
    def test_full_watch_url(self):
        assert parse_youtube_id('https://www.youtube.com/watch?v=dQw4w9WgXcQ') == 'dQw4w9WgXcQ'

    def test_short_url(self):
        assert parse_youtube_id('https://youtu.be/dQw4w9WgXcQ') == 'dQw4w9WgXcQ'

    def test_bare_id(self):
        assert parse_youtube_id('dQw4w9WgXcQ') == 'dQw4w9WgXcQ'

    def test_url_with_extra_params(self):
        assert parse_youtube_id('https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s') == 'dQw4w9WgXcQ'

    def test_embed_url(self):
        assert parse_youtube_id('https://www.youtube.com/embed/dQw4w9WgXcQ') is None

    def test_invalid_returns_none(self):
        # Has invalid characters (spaces, @) — cannot be a YouTube ID
        assert parse_youtube_id('not a video!') is None

    def test_too_short_id_returns_none(self):
        assert parse_youtube_id('abc123') is None

    def test_whitespace_stripped(self):
        assert parse_youtube_id('  dQw4w9WgXcQ  ') == 'dQw4w9WgXcQ'


class TestGetTrailers:
    def test_empty_list(self, client):
        resp = client.get('/api/trailers')
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_returns_added_trailer(self, client):
        add_trailer(client, url='dQw4w9WgXcQ', title='Never Gonna Give')
        data = client.get('/api/trailers').get_json()
        assert len(data) == 1
        assert data[0]['youtube_id'] == 'dQw4w9WgXcQ'
        assert data[0]['title'] == 'Never Gonna Give'
        assert data[0]['active'] is True


class TestAddTrailer:
    def test_add_by_full_url(self, client):
        resp = add_trailer(client, url='https://www.youtube.com/watch?v=dQw4w9WgXcQ')
        assert resp.status_code == 201
        assert resp.get_json()['youtube_id'] == 'dQw4w9WgXcQ'

    def test_add_by_short_url(self, client):
        resp = add_trailer(client, url='https://youtu.be/dQw4w9WgXcQ')
        assert resp.status_code == 201

    def test_add_by_bare_id(self, client):
        resp = add_trailer(client, url='dQw4w9WgXcQ')
        assert resp.status_code == 201

    def test_invalid_url_returns_400(self, client):
        resp = add_trailer(client, url='not-a-video-url')
        assert resp.status_code == 400
        assert 'error' in resp.get_json()

    def test_missing_title_returns_400(self, client):
        resp = client.post('/api/trailers',
                           json={'url': 'dQw4w9WgXcQ', 'title': ''},
                           content_type='application/json')
        assert resp.status_code == 400

    def test_missing_url_returns_400(self, client):
        resp = client.post('/api/trailers',
                           json={'url': '', 'title': 'Test'},
                           content_type='application/json')
        assert resp.status_code == 400

    def test_duplicate_returns_200_not_201(self, client):
        add_trailer(client, url='dQw4w9WgXcQ', title='First')
        resp = add_trailer(client, url='dQw4w9WgXcQ', title='Duplicate')
        assert resp.status_code == 200
        # Should not create a second record
        assert len(client.get('/api/trailers').get_json()) == 1


class TestPatchTrailer:
    def test_update_title(self, client):
        trailer_id = add_trailer(client).get_json()['id']
        resp = client.patch(f'/api/trailers/{trailer_id}',
                            json={'title': 'Updated Title'})
        assert resp.status_code == 200
        assert resp.get_json()['title'] == 'Updated Title'

    def test_toggle_active(self, client):
        trailer_id = add_trailer(client).get_json()['id']
        resp = client.patch(f'/api/trailers/{trailer_id}', json={'active': False})
        assert resp.status_code == 200
        assert resp.get_json()['active'] is False

    def test_nonexistent_returns_404(self, client):
        resp = client.patch('/api/trailers/9999', json={'title': 'x'})
        assert resp.status_code == 404


class TestDeleteTrailer:
    def test_delete_removes_record(self, client):
        trailer_id = add_trailer(client).get_json()['id']
        resp = client.delete(f'/api/trailers/{trailer_id}')
        assert resp.status_code == 204
        assert client.get('/api/trailers').get_json() == []

    def test_delete_nonexistent_returns_404(self, client):
        resp = client.delete('/api/trailers/9999')
        assert resp.status_code == 404


class TestCacheFields:
    """to_dict() exposes cache_status and cached_url."""

    def test_new_trailer_has_null_cache_status(self, client):
        data = add_trailer(client).get_json()
        assert data['cache_status'] is None
        assert data['cached_url'] is None

    def test_cached_url_present_when_ready(self, app, client):
        trailer_id = add_trailer(client).get_json()['id']
        with app.app_context():
            t = db.session.get(Trailer, trailer_id)
            t.cache_status = 'ready'
            t.cached_filename = f'{t.youtube_id}.mp4'
            db.session.commit()
        data = client.get('/api/trailers').get_json()[0]
        assert data['cache_status'] == 'ready'
        assert data['cached_url'] == f'/videos/{data["youtube_id"]}.mp4'

    def test_cached_url_none_when_not_ready(self, app, client):
        trailer_id = add_trailer(client).get_json()['id']
        with app.app_context():
            t = db.session.get(Trailer, trailer_id)
            t.cache_status = 'downloading'
            db.session.commit()
        data = client.get('/api/trailers').get_json()[0]
        assert data['cached_url'] is None

    def test_thumb_url_local_when_ready(self, app, client):
        trailer_id = add_trailer(client, url='dQw4w9WgXcQ').get_json()['id']
        with app.app_context():
            t = db.session.get(Trailer, trailer_id)
            t.cache_status = 'ready'
            t.cached_filename = 'dQw4w9WgXcQ.mp4'
            db.session.commit()
        data = client.get('/api/trailers').get_json()[0]
        assert data['thumb_url'] == '/videos/dQw4w9WgXcQ.jpg'

    def test_thumb_url_youtube_cdn_when_not_cached(self, client):
        add_trailer(client, url='dQw4w9WgXcQ')
        data = client.get('/api/trailers').get_json()[0]
        assert 'img.youtube.com' in data['thumb_url']


class TestDownloadEnqueue:
    """_enqueue_download is called at the right times."""

    def test_add_trailer_enqueues_download(self, client):
        with patch.object(main_module, '_enqueue_download') as mock_enqueue:
            add_trailer(client, url='dQw4w9WgXcQ')
            mock_enqueue.assert_called_once_with('dQw4w9WgXcQ')

    def test_duplicate_add_does_not_enqueue(self, client):
        add_trailer(client, url='dQw4w9WgXcQ')
        with patch.object(main_module, '_enqueue_download') as mock_enqueue:
            add_trailer(client, url='dQw4w9WgXcQ')  # duplicate → 200, no enqueue
            mock_enqueue.assert_not_called()

    def test_next_enqueues_uncached_trailer(self, app, client):
        add_trailer(client, url='dQw4w9WgXcQ')
        frame_id = checkin(client)['frame_id']
        with patch.object(main_module, '_enqueue_download') as mock_enqueue:
            resp = client.get(f'/api/frames/{frame_id}/next')
            assert resp.status_code == 200
            mock_enqueue.assert_called_once_with('dQw4w9WgXcQ')

    def test_next_does_not_enqueue_when_ready(self, app, client):
        trailer_id = add_trailer(client, url='dQw4w9WgXcQ').get_json()['id']
        with app.app_context():
            t = db.session.get(Trailer, trailer_id)
            t.cache_status = 'ready'
            t.cached_filename = 'dQw4w9WgXcQ.mp4'
            db.session.commit()
        frame_id = checkin(client)['frame_id']
        with patch.object(main_module, '_enqueue_download') as mock_enqueue:
            client.get(f'/api/frames/{frame_id}/next')
            mock_enqueue.assert_not_called()

    def test_next_does_not_enqueue_when_pending(self, app, client):
        trailer_id = add_trailer(client, url='dQw4w9WgXcQ').get_json()['id']
        with app.app_context():
            t = db.session.get(Trailer, trailer_id)
            t.cache_status = 'pending'
            db.session.commit()
        frame_id = checkin(client)['frame_id']
        with patch.object(main_module, '_enqueue_download') as mock_enqueue:
            client.get(f'/api/frames/{frame_id}/next')
            mock_enqueue.assert_not_called()

    def test_next_does_not_enqueue_when_downloading(self, app, client):
        trailer_id = add_trailer(client, url='dQw4w9WgXcQ').get_json()['id']
        with app.app_context():
            t = db.session.get(Trailer, trailer_id)
            t.cache_status = 'downloading'
            db.session.commit()
        frame_id = checkin(client)['frame_id']
        with patch.object(main_module, '_enqueue_download') as mock_enqueue:
            client.get(f'/api/frames/{frame_id}/next')
            mock_enqueue.assert_not_called()


class TestNextCachedUrl:
    """cached_url is included in /next response."""

    def test_next_returns_cached_url_when_ready(self, app, client):
        trailer_id = add_trailer(client, url='dQw4w9WgXcQ').get_json()['id']
        with app.app_context():
            t = db.session.get(Trailer, trailer_id)
            t.cache_status = 'ready'
            t.cached_filename = 'dQw4w9WgXcQ.mp4'
            db.session.commit()
        frame_id = checkin(client)['frame_id']
        with patch.object(main_module, '_enqueue_download'):
            data = client.get(f'/api/frames/{frame_id}/next').get_json()
        assert data['type'] == 'trailer'
        assert data['cached_url'] == '/videos/dQw4w9WgXcQ.mp4'

    def test_next_returns_null_cached_url_when_uncached(self, client):
        add_trailer(client, url='dQw4w9WgXcQ')
        frame_id = checkin(client)['frame_id']
        with patch.object(main_module, '_enqueue_download'):
            data = client.get(f'/api/frames/{frame_id}/next').get_json()
        assert data['type'] == 'trailer'
        assert data['cached_url'] is None


class TestServeVideo:
    """GET /videos/<filename> serves files from VIDEOS_DIR."""

    def test_serve_existing_file(self, client):
        path = os.path.join(VIDEOS_DIR, 'test_video.mp4')
        with open(path, 'wb') as f:
            f.write(b'fake mp4 content')
        resp = client.get('/videos/test_video.mp4')
        assert resp.status_code == 200
        assert resp.data == b'fake mp4 content'

    def test_serve_missing_file_returns_404(self, client):
        resp = client.get('/videos/nonexistent.mp4')
        assert resp.status_code == 404


class TestClearCache:
    """DELETE /api/trailers/<id>/cache resets cache and re-enqueues."""

    def test_clear_cache_resets_status(self, app, client):
        trailer_id = add_trailer(client).get_json()['id']
        with app.app_context():
            t = db.session.get(Trailer, trailer_id)
            t.cache_status = 'ready'
            t.cached_filename = f'{t.youtube_id}.mp4'
            db.session.commit()
        with patch.object(main_module, '_enqueue_download'):
            resp = client.delete(f'/api/trailers/{trailer_id}/cache')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['cache_status'] is None
        assert data['cached_url'] is None

    def test_clear_cache_deletes_video_and_thumbnail(self, app, client):
        trailer_id = add_trailer(client, url='dQw4w9WgXcQ').get_json()['id']
        mp4_path = os.path.join(VIDEOS_DIR, 'dQw4w9WgXcQ.mp4')
        jpg_path = os.path.join(VIDEOS_DIR, 'dQw4w9WgXcQ.jpg')
        for path in (mp4_path, jpg_path):
            with open(path, 'wb') as f:
                f.write(b'fake')
        with app.app_context():
            t = db.session.get(Trailer, trailer_id)
            t.cache_status = 'ready'
            t.cached_filename = 'dQw4w9WgXcQ.mp4'
            db.session.commit()
        with patch.object(main_module, '_enqueue_download'):
            client.delete(f'/api/trailers/{trailer_id}/cache')
        assert not os.path.exists(mp4_path)
        assert not os.path.exists(jpg_path)

    def test_clear_cache_reenqueues(self, app, client):
        trailer_id = add_trailer(client, url='dQw4w9WgXcQ').get_json()['id']
        with app.app_context():
            t = db.session.get(Trailer, trailer_id)
            t.cache_status = 'ready'
            t.cached_filename = 'dQw4w9WgXcQ.mp4'
            db.session.commit()
        with patch.object(main_module, '_enqueue_download') as mock_enqueue:
            client.delete(f'/api/trailers/{trailer_id}/cache')
        mock_enqueue.assert_called_once_with('dQw4w9WgXcQ')

    def test_clear_nonexistent_returns_404(self, client):
        resp = client.delete('/api/trailers/9999/cache')
        assert resp.status_code == 404

    def test_delete_trailer_removes_video_and_thumbnail(self, app, client):
        trailer_id = add_trailer(client, url='dQw4w9WgXcQ').get_json()['id']
        mp4_path = os.path.join(VIDEOS_DIR, 'dQw4w9WgXcQ.mp4')
        jpg_path = os.path.join(VIDEOS_DIR, 'dQw4w9WgXcQ.jpg')
        for path in (mp4_path, jpg_path):
            with open(path, 'wb') as f:
                f.write(b'fake')
        with app.app_context():
            t = db.session.get(Trailer, trailer_id)
            t.cache_status = 'ready'
            t.cached_filename = 'dQw4w9WgXcQ.mp4'
            db.session.commit()
        client.delete(f'/api/trailers/{trailer_id}')
        assert not os.path.exists(mp4_path)
        assert not os.path.exists(jpg_path)
