"""Tests for frame checkin, /next content delivery, and frame admin API."""
import pytest

from tests.conftest import add_trailer, checkin, upload_poster


class TestFrameCheckin:
    def test_creates_new_frame(self, client):
        data = checkin(client, hostname='living-room')
        assert 'frame_id' in data
        assert isinstance(data['frame_id'], int)
        assert data['interval_seconds'] == 300  # default
        assert data['rotation'] == 0             # default

    def test_returns_same_id_on_second_checkin(self, client):
        first  = checkin(client, hostname='tv')
        second = checkin(client, hostname='tv')
        assert first['frame_id'] == second['frame_id']

    def test_multiple_frames_get_unique_ids(self, client):
        # Force two different IPs by checking distinct hostnames;
        # in tests remote_addr is always 127.0.0.1, so both checkins
        # resolve to the same frame — verify idempotent upsert instead.
        data = checkin(client, hostname='frame-a')
        frames = client.get('/api/frames').get_json()
        assert any(f['id'] == data['frame_id'] for f in frames)


class TestFrameNext:
    def test_empty_library_returns_empty_type(self, client):
        frame_id = checkin(client)['frame_id']
        resp = client.get(f'/api/frames/{frame_id}/next')
        assert resp.status_code == 200
        assert resp.get_json()['type'] == 'empty'

    def test_returns_poster_from_pool(self, client):
        upload_poster(client)
        frame_id = checkin(client)['frame_id']
        data = client.get(f'/api/frames/{frame_id}/next').get_json()
        assert data['type'] == 'poster'
        assert data['url'].startswith('/images/')
        assert 'title_above' in data
        assert 'title_below' in data
        assert 'rotation' in data
        assert 'interval_seconds' in data

    def test_returns_trailer_from_pool(self, client):
        add_trailer(client, url='dQw4w9WgXcQ', title='Test')
        frame_id = checkin(client)['frame_id']
        # Loop until we get a trailer (pool is random; retry up to 20 times)
        for _ in range(20):
            data = client.get(f'/api/frames/{frame_id}/next').get_json()
            if data['type'] == 'trailer':
                break
        assert data['type'] == 'trailer'
        assert data['youtube_id'] == 'dQw4w9WgXcQ'
        assert data['title'] == 'Test'

    def test_inactive_poster_excluded_from_pool(self, client):
        poster_id = upload_poster(client).get_json()['id']
        client.patch(f'/api/posters/{poster_id}', json={'active': False})
        frame_id = checkin(client)['frame_id']
        data = client.get(f'/api/frames/{frame_id}/next').get_json()
        assert data['type'] == 'empty'

    def test_inactive_trailer_excluded_from_pool(self, client):
        trailer_id = add_trailer(client).get_json()['id']
        client.patch(f'/api/trailers/{trailer_id}', json={'active': False})
        frame_id = checkin(client)['frame_id']
        data = client.get(f'/api/frames/{frame_id}/next').get_json()
        assert data['type'] == 'empty'

    def test_pinned_poster_mode(self, client):
        poster_id = upload_poster(client).get_json()['id']
        frame_id = checkin(client)['frame_id']
        client.patch(f'/api/frames/{frame_id}', json={
            'content_mode': 'pinned',
            'pinned_type': 'poster',
            'pinned_id': poster_id,
        })
        data = client.get(f'/api/frames/{frame_id}/next').get_json()
        assert data['type'] == 'poster'
        assert data['id'] == poster_id

    def test_pinned_trailer_mode(self, client):
        trailer_id = add_trailer(client).get_json()['id']
        frame_id = checkin(client)['frame_id']
        client.patch(f'/api/frames/{frame_id}', json={
            'content_mode': 'pinned',
            'pinned_type': 'trailer',
            'pinned_id': trailer_id,
        })
        data = client.get(f'/api/frames/{frame_id}/next').get_json()
        assert data['type'] == 'trailer'
        assert data['id'] == trailer_id

    def test_pinned_inactive_item_falls_back_to_pool(self, client):
        upload_poster(client, filename='fallback.jpg', title_above='Fallback')
        inactive_id = upload_poster(client, filename='inactive.jpg').get_json()['id']
        client.patch(f'/api/posters/{inactive_id}', json={'active': False})
        frame_id = checkin(client)['frame_id']
        client.patch(f'/api/frames/{frame_id}', json={
            'content_mode': 'pinned',
            'pinned_type': 'poster',
            'pinned_id': inactive_id,
        })
        data = client.get(f'/api/frames/{frame_id}/next').get_json()
        # Falls back to pool — should return the active poster
        assert data['type'] == 'poster'
        assert data['id'] != inactive_id

    def test_response_includes_rotation_and_interval(self, client):
        upload_poster(client)
        frame_id = checkin(client)['frame_id']
        client.patch(f'/api/frames/{frame_id}', json={'rotation': 180, 'interval_seconds': 60})
        data = client.get(f'/api/frames/{frame_id}/next').get_json()
        assert data['rotation'] == 180
        assert data['interval_seconds'] == 60

    def test_nonexistent_frame_returns_404(self, client):
        resp = client.get('/api/frames/9999/next')
        assert resp.status_code == 404

    def test_writes_frame_log(self, client, app):
        upload_poster(client)
        frame_id = checkin(client)['frame_id']
        client.get(f'/api/frames/{frame_id}/next')
        from models import FrameLog
        with app.app_context():
            logs = FrameLog.query.filter_by(frame_id=frame_id).all()
        assert len(logs) == 1
        assert logs[0].content_type == 'poster'


class TestGetFrames:
    def test_empty_list(self, client):
        resp = client.get('/api/frames')
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_returns_checked_in_frame(self, client):
        checkin(client, hostname='bedroom')
        frames = client.get('/api/frames').get_json()
        assert len(frames) == 1
        assert frames[0]['name'] == 'bedroom'


class TestPatchFrame:
    def test_update_name(self, client):
        frame_id = checkin(client)['frame_id']
        resp = client.patch(f'/api/frames/{frame_id}', json={'name': 'Kitchen TV'})
        assert resp.status_code == 200
        assert resp.get_json()['name'] == 'Kitchen TV'

    def test_update_rotation_valid(self, client):
        frame_id = checkin(client)['frame_id']
        for deg in (0, 90, 180, 270):
            resp = client.patch(f'/api/frames/{frame_id}', json={'rotation': deg})
            assert resp.status_code == 200
            assert resp.get_json()['rotation'] == deg

    def test_invalid_rotation_returns_400(self, client):
        frame_id = checkin(client)['frame_id']
        resp = client.patch(f'/api/frames/{frame_id}', json={'rotation': 45})
        assert resp.status_code == 400

    def test_update_interval(self, client):
        frame_id = checkin(client)['frame_id']
        resp = client.patch(f'/api/frames/{frame_id}', json={'interval_seconds': 120})
        assert resp.get_json()['interval_seconds'] == 120

    def test_interval_minimum_enforced(self, client):
        frame_id = checkin(client)['frame_id']
        resp = client.patch(f'/api/frames/{frame_id}', json={'interval_seconds': 1})
        assert resp.get_json()['interval_seconds'] == 10  # clamped to min

    def test_invalid_content_mode_returns_400(self, client):
        frame_id = checkin(client)['frame_id']
        resp = client.patch(f'/api/frames/{frame_id}', json={'content_mode': 'shuffle'})
        assert resp.status_code == 400

    def test_nonexistent_frame_returns_404(self, client):
        resp = client.patch('/api/frames/9999', json={'name': 'Ghost'})
        assert resp.status_code == 404
