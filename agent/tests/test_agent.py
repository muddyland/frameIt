"""Tests for all FrameIT agent endpoints."""
import subprocess
from unittest.mock import MagicMock, patch

import pytest


# ── Auth ──────────────────────────────────────────────────────────────────

class TestAuth:
    def test_missing_auth_returns_401(self, client):
        resp = client.get('/health')
        assert resp.status_code == 401

    def test_wrong_token_returns_401(self, client, bad_headers):
        resp = client.get('/health', headers=bad_headers)
        assert resp.status_code == 401

    def test_correct_token_returns_200(self, client, auth_headers):
        resp = client.get('/health', headers=auth_headers)
        assert resp.status_code == 200


# ── Health ────────────────────────────────────────────────────────────────

class TestHealth:
    def test_health_response_shape(self, client, auth_headers):
        resp = client.get('/health', headers=auth_headers)
        body = resp.get_json()
        assert body['ok'] is True
        assert 'hostname' in body
        assert 'uptime_seconds' in body
        assert 'version' in body


# ── System info ───────────────────────────────────────────────────────────

class TestSystemInfo:
    def test_returns_expected_fields(self, client, auth_headers):
        resp = client.get('/system/info', headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert 'cpu_percent' in body
        assert 'ram_percent' in body
        assert 'disk_percent' in body
        assert 'hostname' in body
        assert 'uptime_seconds' in body

    def test_percentages_are_numeric(self, client, auth_headers):
        body = client.get('/system/info', headers=auth_headers).get_json()
        assert isinstance(body['cpu_percent'], (int, float))
        assert isinstance(body['ram_percent'], (int, float))
        assert isinstance(body['disk_percent'], (int, float))


# ── Reboot ────────────────────────────────────────────────────────────────

class TestReboot:
    def test_reboot_returns_200(self, client, auth_headers):
        with patch('agent.agent.subprocess.run'), \
             patch('agent.agent.threading.Thread') as mock_thread:
            mock_thread.return_value.start = MagicMock()
            resp = client.post('/system/reboot', headers=auth_headers)
        assert resp.status_code == 200
        assert 'Rebooting' in resp.get_json()['message']

    def test_reboot_requires_post(self, client, auth_headers):
        resp = client.get('/system/reboot', headers=auth_headers)
        assert resp.status_code == 405


# ── apt update / upgrade ──────────────────────────────────────────────────

class TestAptCommands:
    def _fake_popen(self, cmd, **kwargs):
        proc = MagicMock()
        proc.stdout = iter(['fake output\n'])
        proc.__enter__ = lambda s: s
        proc.__exit__ = MagicMock(return_value=False)
        return proc

    def test_apt_update_calls_apt(self, client, auth_headers):
        with patch('agent.agent.subprocess.Popen', side_effect=self._fake_popen) as mock_popen:
            resp = client.post('/system/update', headers=auth_headers)
        assert resp.status_code == 200
        assert 'text/plain' in resp.content_type
        cmd_used = mock_popen.call_args[0][0]
        assert 'apt-get' in cmd_used
        assert 'update' in cmd_used

    def test_apt_upgrade_calls_apt(self, client, auth_headers):
        with patch('agent.agent.subprocess.Popen', side_effect=self._fake_popen) as mock_popen:
            resp = client.post('/system/upgrade', headers=auth_headers)
        assert resp.status_code == 200
        cmd_used = mock_popen.call_args[0][0]
        assert 'upgrade' in cmd_used


# ── Service status / restart ──────────────────────────────────────────────

class TestServices:
    def _mock_run_active(self, cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stderr = ''
        return result

    def test_service_status_returns_dict(self, client, auth_headers):
        with patch('agent.agent.subprocess.run', side_effect=self._mock_run_active):
            resp = client.get('/system/services', headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert 'frameit-agent' in body
        assert 'frameit-ui' in body

    def test_restart_known_service(self, client, auth_headers):
        with patch('agent.agent.subprocess.run', side_effect=self._mock_run_active):
            resp = client.post('/system/services/frameit-ui/restart', headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()['ok'] is True

    def test_restart_unknown_service_returns_400(self, client, auth_headers):
        resp = client.post('/system/services/evil-service/restart', headers=auth_headers)
        assert resp.status_code == 400


# ── Network ───────────────────────────────────────────────────────────────

class TestNetwork:
    def test_network_status_returns_expected_fields(self, client, auth_headers):
        resp = client.get('/network/status', headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert 'hostname' in body
        assert 'interfaces' in body
        assert isinstance(body['interfaces'], list)

    def test_wifi_scan_returns_networks(self, client, auth_headers):
        def mock_run(cmd, **kwargs):
            r = MagicMock()
            r.stdout = 'HomeNetwork:75\nGuestNet:60\n'
            r.returncode = 0
            return r
        with patch('agent.agent.subprocess.run', side_effect=mock_run):
            resp = client.get('/network/wifi/scan', headers=auth_headers)
        assert resp.status_code == 200
        assert 'networks' in resp.get_json()

    def test_wifi_connect_missing_ssid_returns_400(self, client, auth_headers):
        resp = client.post('/network/wifi/connect',
                           json={'ssid': '', 'password': 'pass'},
                           headers=auth_headers)
        assert resp.status_code == 400

    def test_wifi_connect_success(self, client, auth_headers):
        def mock_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = 'Device connected\n'
            return r
        with patch('agent.agent.subprocess.run', side_effect=mock_run):
            resp = client.post('/network/wifi/connect',
                               json={'ssid': 'MyWifi', 'password': 'secret'},
                               headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()['ok'] is True


# ── Display ───────────────────────────────────────────────────────────────

class TestDisplay:
    def _xset_run(self, monitor_on=True):
        def _run(cmd, **kwargs):
            r = MagicMock()
            r.stdout = 'DPMS is Disabled\n' if monitor_on else 'Monitor is Off\n'
            r.returncode = 0
            r.stderr = ''
            return r
        return _run

    def test_display_status_on(self, client, auth_headers):
        with patch('agent.agent.subprocess.run', side_effect=self._xset_run(monitor_on=True)):
            resp = client.get('/display', headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()['on'] is True

    def test_display_status_off(self, client, auth_headers):
        with patch('agent.agent.subprocess.run', side_effect=self._xset_run(monitor_on=False)):
            resp = client.get('/display', headers=auth_headers)
        assert resp.get_json()['on'] is False

    def test_display_on(self, client, auth_headers):
        with patch('agent.agent.subprocess.run', return_value=MagicMock(returncode=0)):
            resp = client.post('/display/on', headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()['ok'] is True

    def test_display_off(self, client, auth_headers):
        with patch('agent.agent.subprocess.run', return_value=MagicMock(returncode=0)):
            resp = client.post('/display/off', headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()['ok'] is True
