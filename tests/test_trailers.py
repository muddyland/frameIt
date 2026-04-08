"""Tests for the Trailer CRUD API and YouTube ID parsing."""
import pytest

from main import parse_youtube_id
from tests.conftest import add_trailer


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
