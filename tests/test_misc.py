"""Tests for miscellaneous routes: frame display, install script, manifest."""


class TestFrameDisplay:
    def test_root_returns_200(self, client):
        resp = client.get('/')
        assert resp.status_code == 200

    def test_root_returns_html(self, client):
        resp = client.get('/')
        assert b'frameId' in resp.data
        assert b'fetchNext' in resp.data

    def test_root_contains_checkin_call(self, client):
        resp = client.get('/')
        assert b'/api/frames/checkin' in resp.data

    def test_root_contains_youtube_api(self, client):
        resp = client.get('/')
        assert b'youtube.com/player_api' in resp.data


class TestManifest:
    def test_manifest_returns_json(self, client):
        resp = client.get('/manifest.json')
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['name'] == 'FrameIT'
        assert body['short_name'] == 'FrameIT'

    def test_manifest_has_required_pwa_fields(self, client):
        body = client.get('/manifest.json').get_json()
        assert 'start_url' in body
        assert 'display' in body


class TestInstallScript:
    def test_returns_200(self, client):
        resp = client.get('/install.sh')
        assert resp.status_code == 200

    def test_content_type_is_plain_text(self, client):
        resp = client.get('/install.sh')
        assert 'text/plain' in resp.content_type

    def test_downloads_agent_py(self, client):
        resp = client.get('/install.sh')
        assert b'agent.py' in resp.data
        assert b'agent-requirements.txt' in resp.data

    def test_contains_systemd_install(self, client):
        resp = client.get('/install.sh')
        assert b'systemctl' in resp.data
        assert b'frameit-agent' in resp.data

    def test_contains_arg_parsing(self, client):
        resp = client.get('/install.sh')
        assert b'--server' in resp.data
        assert b'--token' in resp.data


class TestAdminRoutes:
    def test_admin_index(self, client):
        resp = client.get('/admin')
        assert resp.status_code == 200

    def test_admin_posters(self, client):
        resp = client.get('/admin/posters')
        assert resp.status_code == 200

    def test_admin_trailers(self, client):
        resp = client.get('/admin/trailers')
        assert resp.status_code == 200

    def test_admin_frames(self, client):
        resp = client.get('/admin/frames')
        assert resp.status_code == 200

    def test_admin_tokens(self, client):
        # /admin/tokens was merged into /admin/frames — expect redirect
        resp = client.get('/admin/tokens')
        assert resp.status_code == 302
        assert '/admin/frames' in resp.headers['Location']
