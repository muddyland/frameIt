"""Tests for the authentication, CSRF and credential hardening."""
import os
import stat

import pytest

import main as main_module
from models import db, Frame, Settings, Trailer
from tests.conftest import checkin, enable_bypass


class TestCSRF:
    """State-changing requests must present the session's CSRF token."""

    def test_post_with_wrong_token_rejected(self, client):
        """An authenticated session is not on its own enough to mutate state."""
        resp = client.post('/api/tokens', headers={'X-CSRF-Token': 'wrong-value'})
        assert resp.status_code == 400
        assert 'CSRF' in resp.get_json()['error']

    def test_post_with_valid_token_accepted(self, client):
        assert client.post('/api/tokens').status_code == 201

    def test_delete_without_token_rejected(self, client):
        token_id = client.post('/api/tokens').get_json()['id']
        resp = client.delete(f'/api/tokens/{token_id}',
                             headers={'X-CSRF-Token': 'nope'})
        assert resp.status_code == 400

    def test_get_requests_need_no_token(self, client):
        assert client.get('/api/posters', headers={'X-CSRF-Token': 'nope'}).status_code == 200

    def test_agent_endpoints_are_exempt(self, raw_client):
        """Agents are not browsers and carry no session cookie."""
        resp = raw_client.post('/api/agents/register',
                               json={'token': 'x' * 64, 'hostname': 'pi'})
        # 401 for the bad token, not 400 for a missing CSRF token.
        assert resp.status_code == 401

    def test_login_form_carries_a_token(self, raw_client):
        html = raw_client.get('/admin/login').get_data(as_text=True)
        assert '_csrf_token' in html


class TestSessionInvalidation:
    def test_password_change_invalidates_other_sessions(self, app, client):
        other = app.test_client()
        other.post('/admin/login', data={'username': 'admin', 'password': 'testpass'})
        assert other.get('/api/posters').status_code == 200

        resp = client.post('/admin/password',
                           json={'current': 'testpass', 'new': 'a-much-longer-password'})
        assert resp.status_code == 200

        # The other session's fingerprint no longer matches the stored hash.
        assert other.get('/api/posters').status_code == 401
        # The session that changed it stays signed in.
        assert client.get('/api/posters').status_code == 200

    def test_short_password_rejected(self, client):
        resp = client.post('/admin/password', json={'current': 'testpass', 'new': 'short'})
        assert resp.status_code == 400
        assert 'at least' in resp.get_json()['error']

    def test_non_string_password_rejected(self, client):
        resp = client.post('/admin/password', json={'current': 'testpass', 'new': 12345})
        assert resp.status_code == 400


class TestLoginThrottle:
    def test_lockout_after_repeated_failures(self, app, client):
        main_module._login_attempts.clear()
        attacker = app.test_client()
        codes = []
        for _ in range(main_module.LOGIN_MAX_ATTEMPTS + 2):
            r = attacker.post('/admin/login',
                              data={'username': 'admin', 'password': 'wrong'})
            codes.append(r.status_code)
        assert 429 in codes, 'expected a lockout after repeated failures'
        main_module._login_attempts.clear()

    def test_successful_login_clears_counter(self, app, client):
        main_module._login_attempts.clear()
        user = app.test_client()
        user.post('/admin/login', data={'username': 'admin', 'password': 'wrong'})
        resp = user.post('/admin/login', data={'username': 'admin', 'password': 'testpass'})
        assert resp.status_code == 302
        assert not main_module._login_attempts

    def test_open_redirect_blocked(self, app, client):
        user = app.test_client()
        resp = user.post('/admin/login?next=https://evil.example/x',
                         data={'username': 'admin', 'password': 'testpass'})
        assert resp.status_code == 302
        assert 'evil.example' not in resp.headers['Location']


class TestSecurityHeaders:
    def test_headers_present(self, client):
        resp = client.get('/admin')
        assert resp.headers['X-Content-Type-Options'] == 'nosniff'
        assert 'Content-Security-Policy' in resp.headers
        assert "frame-ancestors 'self'" in resp.headers['Content-Security-Policy']

    def test_session_cookie_flags(self, app):
        assert app.config['SESSION_COOKIE_HTTPONLY'] is True
        assert app.config['SESSION_COOKIE_SAMESITE'] == 'Lax'


class TestSecretKeyPermissions:
    def test_key_file_is_owner_only(self):
        path = os.path.join(os.environ['DATA_DIR'], 'secret.key')
        assert os.path.exists(path)
        assert stat.S_IMODE(os.stat(path).st_mode) & 0o077 == 0

    def test_existing_loose_key_is_tightened(self, tmp_path):
        key = tmp_path / 'secret.key'
        key.write_text('deadbeef')
        os.chmod(key, 0o644)
        value = main_module._read_or_create_secret_key(str(key))
        assert value == 'deadbeef', 'must not rotate the key and log everyone out'
        assert stat.S_IMODE(os.stat(key).st_mode) & 0o077 == 0


class TestUploadValidation:
    def test_extension_alone_is_not_enough(self, client):
        import io
        data = {'file': (io.BytesIO(b'<script>alert(1)</script>'), 'evil.jpg')}
        resp = client.post('/api/posters/upload', data=data,
                           content_type='multipart/form-data')
        assert resp.status_code == 400
        assert 'not a valid' in resp.get_json()['error']

    def test_max_content_length_configured(self, app):
        assert app.config['MAX_CONTENT_LENGTH'] == main_module.MAX_UPLOAD_BYTES

    @pytest.mark.parametrize('head,expected', [
        (b'\xff\xd8\xff\xe0rest', 'jpeg'),
        (b'\x89PNG\r\n\x1a\nrest', 'png'),
        (b'RIFF\x00\x00\x00\x00WEBPVP8 ', 'webp'),
        (b'GIF89a', None),
        (b'', None),
    ])
    def test_sniff_image_format(self, head, expected):
        assert main_module.sniff_image_format(head) == expected


class TestAgentRegistrationHardening:
    def test_port_must_be_a_number(self, client):
        token = client.post('/api/tokens').get_json()['token']
        resp = client.post('/api/agents/register',
                           json={'token': token, 'hostname': 'pi', 'port': '5001@evil.example'})
        assert resp.status_code == 400

    def test_port_range_enforced(self, client):
        token = client.post('/api/tokens').get_json()['token']
        resp = client.post('/api/agents/register',
                           json={'token': token, 'hostname': 'pi', 'port': 99999})
        assert resp.status_code == 400

    def test_agent_url_cannot_be_redirected(self, app, client):
        token = client.post('/api/tokens').get_json()['token']
        frame_id = client.post('/api/agents/register',
                               json={'token': token, 'hostname': 'pi', 'port': 5001},
                               ).get_json()['frame_id']
        with app.app_context():
            assert db.session.get(Frame, frame_id).agent_url == 'http://127.0.0.1:5001'

    def test_hostile_hostname_is_rejected(self, app, client):
        token = client.post('/api/tokens').get_json()['token']
        payload = '<img src=x onerror=alert(1)>'
        frame_id = client.post('/api/agents/register',
                               json={'token': token, 'hostname': payload, 'port': 5001},
                               ).get_json()['frame_id']
        with app.app_context():
            assert db.session.get(Frame, frame_id).name != payload

    def test_secret_issued_when_requested(self, app, client):
        token = client.post('/api/tokens').get_json()['token']
        body = client.post('/api/agents/register',
                           json={'token': token, 'hostname': 'pi', 'port': 5001,
                                 'supports_secret': True}).get_json()
        assert len(body.get('agent_secret', '')) == 64
        with app.app_context():
            frame = db.session.get(Frame, body['frame_id'])
            assert frame.agent_secret == body['agent_secret']
            # The proxy must prefer the dedicated credential.
            assert frame.credential() == frame.agent_secret

    def test_legacy_agent_keeps_working(self, app, client):
        """An agent that predates this release gets no secret and still works."""
        token = client.post('/api/tokens').get_json()['token']
        body = client.post('/api/agents/register',
                           json={'token': token, 'hostname': 'pi', 'port': 5001}).get_json()
        assert 'agent_secret' not in body
        with app.app_context():
            frame = db.session.get(Frame, body['frame_id'])
            assert frame.agent_secret is None
            assert frame.credential() == token


class TestAgentHeartbeatAuth:
    def _register(self, client, with_secret=True):
        token = client.post('/api/tokens').get_json()['token']
        return client.post('/api/agents/register',
                           json={'token': token, 'hostname': 'pi', 'port': 5001,
                                 'supports_secret': with_secret}).get_json()

    def test_valid_credential_accepted(self, client):
        reg = self._register(client)
        resp = client.post(f"/api/agents/{reg['frame_id']}/heartbeat",
                           headers={'Authorization': f"Bearer {reg['agent_secret']}"})
        assert resp.status_code == 200

    def test_wrong_credential_always_rejected(self, client):
        reg = self._register(client)
        resp = client.post(f"/api/agents/{reg['frame_id']}/heartbeat",
                           headers={'Authorization': 'Bearer nope'})
        assert resp.status_code == 401

    def test_missing_credential_allowed_in_grace_mode(self, client):
        """Legacy agents send no header; upgrading must not orphan them."""
        reg = self._register(client, with_secret=False)
        resp = client.post(f"/api/agents/{reg['frame_id']}/heartbeat")
        assert resp.status_code == 200

    def test_missing_credential_rejected_in_strict_mode(self, app, client):
        reg = self._register(client)
        client.patch('/api/settings', json={'strict_agent_auth': True})
        resp = client.post(f"/api/agents/{reg['frame_id']}/heartbeat")
        assert resp.status_code == 401

    def test_readiness_reports_legacy_agents(self, client):
        self._register(client, with_secret=False)
        body = client.get('/api/settings/agent-auth-readiness').get_json()
        assert body['ready'] is False
        assert len(body['legacy_agents']) == 1

        self._register(client, with_secret=True)
        body = client.get('/api/settings/agent-auth-readiness').get_json()
        assert body['ready'] is True


class TestFrameTokenAuth:
    def test_checkin_issues_a_token(self, client):
        data = checkin(client)
        assert len(data['frame_token']) == 32

    def test_next_open_in_grace_mode(self, client):
        frame_id = checkin(client)['frame_id']
        assert client.get(f'/api/frames/{frame_id}/next').status_code == 200

    def test_next_requires_token_in_strict_mode(self, client):
        data = checkin(client)
        frame_id, token = data['frame_id'], data['frame_token']
        client.patch('/api/settings', json={'strict_frame_auth': True})

        assert client.get(f'/api/frames/{frame_id}/next').status_code == 401
        assert client.get(f'/api/frames/{frame_id}/next?t=wrong').status_code == 401
        assert client.get(f'/api/frames/{frame_id}/next?t={token}').status_code == 200

    def test_signal_requires_token_in_strict_mode(self, client):
        data = checkin(client)
        client.patch('/api/settings', json={'strict_frame_auth': True})
        assert client.get(f"/api/frames/{data['frame_id']}/signal").status_code == 401
        assert client.get(
            f"/api/frames/{data['frame_id']}/signal?t={data['frame_token']}").status_code == 200

    def test_token_is_frame_specific(self, client):
        first = checkin(client)
        with client.application.app_context():
            other = Frame(ip='10.9.9.9', name='other')
            db.session.add(other)
            db.session.commit()
            other_id = other.id
        client.patch('/api/settings', json={'strict_frame_auth': True})
        resp = client.get(f"/api/frames/{other_id}/next?t={first['frame_token']}")
        assert resp.status_code == 401


class TestBypassGating:
    def test_bypass_disabled_by_default(self, client):
        resp = client.post('/api/frames/checkin', json={'hostname': 'x', 'bypass': True})
        assert resp.get_json() == {'registered': False}

    def test_bypass_works_when_enabled(self, app, client):
        with app.app_context():
            enable_bypass()
        resp = client.post('/api/frames/checkin', json={'hostname': 'x', 'bypass': True})
        assert resp.get_json()['registered'] is True

    def test_bypass_name_is_validated(self, app, client):
        with app.app_context():
            enable_bypass()
        client.post('/api/frames/checkin',
                    json={'hostname': '<script>alert(1)</script>', 'bypass': True})
        with app.app_context():
            frame = Frame.query.first()
            assert '<script>' not in (frame.name or '')


class TestAgentProxyAllowlist:
    def test_only_known_subpaths_are_relayed(self):
        assert main_module._agent_subpath_allowed('system/info')
        assert main_module._agent_subpath_allowed('system/services/frameit-ui/restart')
        assert not main_module._agent_subpath_allowed('system/services/sshd/stop')
        assert not main_module._agent_subpath_allowed('../../secret')
        assert not main_module._agent_subpath_allowed('shell')
