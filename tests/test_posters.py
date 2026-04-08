"""Tests for the Poster CRUD API."""
import io
import json
import os

import pytest

from tests.conftest import upload_poster


class TestGetPosters:
    def test_empty_list(self, client):
        resp = client.get('/api/posters')
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_returns_uploaded_poster(self, client):
        upload_poster(client)
        resp = client.get('/api/posters')
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]['title_above'] == 'Now Playing'
        assert data[0]['title_below'] == 'In Theaters'
        assert data[0]['active'] is True
        assert data[0]['url'].startswith('/images/')

    def test_returns_multiple_posters(self, client):
        upload_poster(client, filename='a.jpg')
        upload_poster(client, filename='b.jpg')
        data = client.get('/api/posters').get_json()
        assert len(data) == 2


class TestUploadPoster:
    def test_valid_jpg(self, client):
        resp = upload_poster(client, filename='movie.jpg')
        assert resp.status_code == 201
        body = resp.get_json()
        assert 'id' in body
        assert body['title_above'] == 'Now Playing'
        assert body['title_below'] == 'In Theaters'
        assert body['active'] is True

    def test_valid_png(self, client):
        resp = upload_poster(client, filename='movie.png')
        assert resp.status_code == 201

    def test_valid_webp(self, client):
        resp = upload_poster(client, filename='movie.webp')
        assert resp.status_code == 201

    def test_invalid_extension_rejected(self, client):
        data = {'file': (io.BytesIO(b'data'), 'movie.gif')}
        resp = client.post('/api/posters/upload', data=data,
                           content_type='multipart/form-data')
        assert resp.status_code == 400
        assert 'error' in resp.get_json()

    def test_no_file_rejected(self, client):
        resp = client.post('/api/posters/upload',
                           data={'title_above': 'test'},
                           content_type='multipart/form-data')
        assert resp.status_code == 400

    def test_filename_uses_uuid_prefix(self, client):
        upload_poster(client, filename='poster.jpg')
        body = client.get('/api/posters').get_json()[0]
        # UUID prefix + underscore + original name
        assert '_poster.jpg' in body['filename']
        assert len(body['filename']) > len('poster.jpg')

    def test_file_is_written_to_disk(self, client):
        images_dir = os.environ['IMAGES_DIR']
        before = set(os.listdir(images_dir))
        upload_poster(client, filename='disk_test.jpg')
        after = set(os.listdir(images_dir))
        new_files = after - before
        assert len(new_files) == 1

    def test_optional_fields_default_to_none(self, client):
        data = {'file': (io.BytesIO(b'FAKEJPEG'), 'bare.jpg')}
        resp = client.post('/api/posters/upload', data=data,
                           content_type='multipart/form-data')
        assert resp.status_code == 201
        body = resp.get_json()
        assert body['title_above'] is None
        assert body['title_below'] is None


class TestPatchPoster:
    def test_update_titles(self, client):
        poster_id = upload_poster(client).get_json()['id']
        resp = client.patch(f'/api/posters/{poster_id}',
                            json={'title_above': 'Updated', 'title_below': 'New Sub'})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['title_above'] == 'Updated'
        assert body['title_below'] == 'New Sub'

    def test_toggle_active(self, client):
        poster_id = upload_poster(client).get_json()['id']
        resp = client.patch(f'/api/posters/{poster_id}', json={'active': False})
        assert resp.status_code == 200
        assert resp.get_json()['active'] is False

    def test_update_sort_order(self, client):
        poster_id = upload_poster(client).get_json()['id']
        resp = client.patch(f'/api/posters/{poster_id}', json={'sort_order': 5})
        assert resp.status_code == 200
        assert resp.get_json()['sort_order'] == 5

    def test_nonexistent_poster_returns_404(self, client):
        resp = client.patch('/api/posters/9999', json={'title_above': 'x'})
        assert resp.status_code == 404

    def test_empty_string_title_becomes_none(self, client):
        poster_id = upload_poster(client).get_json()['id']
        resp = client.patch(f'/api/posters/{poster_id}', json={'title_above': ''})
        assert resp.get_json()['title_above'] is None


class TestDeletePoster:
    def test_delete_removes_from_db(self, client):
        poster_id = upload_poster(client).get_json()['id']
        resp = client.delete(f'/api/posters/{poster_id}')
        assert resp.status_code == 204
        assert client.get('/api/posters').get_json() == []

    def test_delete_removes_file_from_disk(self, client):
        images_dir = os.environ['IMAGES_DIR']
        poster_id = upload_poster(client).get_json()['id']
        filename = client.get('/api/posters').get_json()[0]['filename']
        filepath = os.path.join(images_dir, filename)
        assert os.path.exists(filepath)
        client.delete(f'/api/posters/{poster_id}')
        assert not os.path.exists(filepath)

    def test_delete_nonexistent_returns_404(self, client):
        resp = client.delete('/api/posters/9999')
        assert resp.status_code == 404
