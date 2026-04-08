"""Tests for registration tokens and agent registration/proxy."""
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import checkin


class TestTokens:
    def test_generate_token(self, client):
        resp = client.post('/api/tokens')
        assert resp.status_code == 201
        body = resp.get_json()
        assert 'token' in body
        assert len(body['token']) == 64          # 32 bytes hex
        assert 'install_cmd' in body
        assert '--token' in body['install_cmd']

    def test_list_tokens_empty(self, client):
        resp = client.get('/api/tokens')
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_list_tokens_shows_generated(self, client):
        client.post('/api/tokens')
        tokens = client.get('/api/tokens').get_json()
        assert len(tokens) == 1
        assert tokens[0]['used_at'] is None

    def test_revoke_unused_token(self, client):
        token_id = client.post('/api/tokens').get_json()['id']
        resp = client.delete(f'/api/tokens/{token_id}')
        assert resp.status_code == 204
        assert client.get('/api/tokens').get_json() == []

    def test_revoke_nonexistent_returns_404(self, client):
        resp = client.delete('/api/tokens/9999')
        assert resp.status_code == 404

    def test_cannot_revoke_used_token(self, client):
        token_val = client.post('/api/tokens').get_json()['token']
        token_id  = client.get('/api/tokens').get_json()[0]['id']
        # Use the token via agent register
        client.post('/api/agents/register',
                    json={'token': token_val, 'hostname': 'pi', 'port': 5001},
                    content_type='application/json')
        resp = client.delete(f'/api/tokens/{token_id}')
        assert resp.status_code == 400

    def test_install_cmd_contains_server_url(self, client):
        body = client.post('/api/tokens').get_json()
        assert 'localhost' in body['install_cmd'] or 'http' in body['install_cmd']


class TestAgentRegister:
    def test_valid_token_registers_agent(self, client):
        token_val = client.post('/api/tokens').get_json()['token']
        resp = client.post('/api/agents/register',
                           json={'token': token_val, 'hostname': 'mypi', 'port': 5001},
                           content_type='application/json')
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['ok'] is True
        assert 'frame_id' in body

    def test_register_sets_agent_url_on_frame(self, client, app):
        token_val = client.post('/api/tokens').get_json()['token']
        frame_id = client.post('/api/agents/register',
                               json={'token': token_val, 'hostname': 'mypi', 'port': 5001},
                               content_type='application/json').get_json()['frame_id']
        from models import Frame, db as _db
        with app.app_context():
            frame = _db.session.get(Frame, frame_id)
            assert frame.agent_url is not None
            assert ':5001' in frame.agent_url

    def test_register_marks_token_used(self, client):
        token_val = client.post('/api/tokens').get_json()['token']
        client.post('/api/agents/register',
                    json={'token': token_val, 'hostname': 'mypi', 'port': 5001})
        tokens = client.get('/api/tokens').get_json()
        assert tokens[0]['used_at'] is not None

    def test_invalid_token_returns_401(self, client):
        resp = client.post('/api/agents/register',
                           json={'token': 'bad_token', 'hostname': 'pi', 'port': 5001})
        assert resp.status_code == 401

    def test_same_ip_reregister_allowed(self, client):
        # Agent restart / post-update re-registration from same IP must succeed
        token_val = client.post('/api/tokens').get_json()['token']
        client.post('/api/agents/register',
                    json={'token': token_val, 'hostname': 'pi', 'port': 5001})
        resp = client.post('/api/agents/register',
                           json={'token': token_val, 'hostname': 'pi', 'port': 5001})
        assert resp.status_code == 200
        assert 'frame_id' in resp.get_json()

    def test_different_ip_used_token_returns_401(self, client):
        # A different machine cannot hijack an already-used token
        token_val = client.post('/api/tokens').get_json()['token']
        client.post('/api/agents/register',
                    json={'token': token_val, 'hostname': 'pi', 'port': 5001})
        resp = client.post('/api/agents/register',
                           json={'token': token_val, 'hostname': 'pi', 'port': 5001},
                           environ_base={'REMOTE_ADDR': '10.0.0.99'})
        assert resp.status_code == 401

    def test_register_creates_frame_if_not_exists(self, client):
        token_val = client.post('/api/tokens').get_json()['token']
        client.post('/api/agents/register',
                    json={'token': token_val, 'hostname': 'newpi', 'port': 5001})
        frames = client.get('/api/frames').get_json()
        assert len(frames) == 1
        assert frames[0]['name'] == 'newpi'

    def test_register_updates_existing_frame(self, client):
        # Frame already exists from a prior checkin
        frame_id = checkin(client, hostname='existing')['frame_id']
        token_val = client.post('/api/tokens').get_json()['token']
        reg = client.post('/api/agents/register',
                          json={'token': token_val, 'hostname': 'existing', 'port': 5001})
        assert reg.get_json()['frame_id'] == frame_id


class TestAgentHeartbeat:
    def test_heartbeat_updates_last_seen(self, client, app):
        token_val = client.post('/api/tokens').get_json()['token']
        frame_id = client.post('/api/agents/register',
                               json={'token': token_val, 'hostname': 'pi', 'port': 5001},
                               ).get_json()['frame_id']
        resp = client.post(f'/api/agents/{frame_id}/heartbeat')
        assert resp.status_code == 200
        body = resp.get_json()
        assert 'interval_seconds' in body
        assert 'rotation' in body

    def test_heartbeat_nonexistent_frame_returns_404(self, client):
        resp = client.post('/api/agents/9999/heartbeat')
        assert resp.status_code == 404


class TestAgentProxy:
    def test_proxy_no_agent_returns_404(self, client):
        frame_id = checkin(client)['frame_id']
        resp = client.post(f'/api/frames/{frame_id}/agent/system/reboot')
        assert resp.status_code == 404

    def test_proxy_agent_unreachable_returns_503(self, client):
        token_val = client.post('/api/tokens').get_json()['token']
        frame_id = client.post('/api/agents/register',
                               json={'token': token_val, 'hostname': 'pi', 'port': 5001},
                               ).get_json()['frame_id']
        # Agent registered but not actually running — connection refused → 503
        resp = client.get(f'/api/frames/{frame_id}/agent/health')
        assert resp.status_code == 503

    def test_proxy_forwards_to_agent(self, client):
        token_val = client.post('/api/tokens').get_json()['token']
        frame_id = client.post('/api/agents/register',
                               json={'token': token_val, 'hostname': 'pi', 'port': 5001},
                               ).get_json()['frame_id']
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {'Content-Type': 'application/json'}
        mock_resp.iter_content = lambda chunk_size: [b'{"ok":true}']

        with patch('main.http_requests.request', return_value=mock_resp) as mock_req:
            resp = client.get(f'/api/frames/{frame_id}/agent/health')
            assert resp.status_code == 200
            assert mock_req.called
            call_kwargs = mock_req.call_args
            assert 'Bearer' in call_kwargs.kwargs.get('headers', {}).get('Authorization', '')
