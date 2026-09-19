"""Tests for the Now Playing webhook, its token, and the /next override.

The webhook is machine-to-machine: no admin session, no CSRF token, and a
bearer token that the server generates once and never hands back. These tests
cover both halves of that — that a caller with the token gets in, and that
everything else stays out.
"""
import io
from datetime import timedelta

from models import db, Frame, NowPlaying, Settings, utcnow
from tests.conftest import JPEG_BYTES, PNG_BYTES, checkin, upload_poster


def generate_token(client):
    """Mint a webhook token through the admin endpoint and return the value."""
    resp = client.post('/api/settings/now-playing-token')
    assert resp.status_code == 200
    return resp.get_json()['token']


def post_now_playing(http, token, state='playing', image=JPEG_BYTES,
                     filename='art.jpg', **fields):
    """POST a now-playing update the way the HA integration would."""
    data = {'state': state}
    data.update(fields)
    if image is not None:
        data['image'] = (io.BytesIO(image), filename)
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    return http.post('/api/now-playing', data=data, headers=headers,
                     content_type='multipart/form-data')


def set_now_playing_flag(app, frame_id, value=True):
    with app.app_context():
        db.session.get(Frame, frame_id).show_now_playing = value
        db.session.commit()


def backdate(app, seconds):
    """Age the now-playing row, as the heartbeat tests age last_seen."""
    with app.app_context():
        np = db.session.get(NowPlaying, 1)
        np.updated_at = utcnow() - timedelta(seconds=seconds)
        db.session.commit()


class TestWebhookAuth:
    def test_rejected_when_no_token_configured(self, client):
        """The endpoint is inert until an admin generates a token."""
        resp = post_now_playing(client, 'anything-at-all')
        assert resp.status_code == 401

    def test_rejected_without_authorization_header(self, client):
        generate_token(client)
        resp = post_now_playing(client, None)
        assert resp.status_code == 401

    def test_rejected_with_wrong_token(self, client):
        generate_token(client)
        resp = post_now_playing(client, 'f' * 64)
        assert resp.status_code == 401

    def test_accepted_with_correct_token(self, client):
        token = generate_token(client)
        resp = post_now_playing(client, token)
        assert resp.status_code == 200
        assert resp.get_json()['ok'] is True

    def test_needs_no_admin_session_or_csrf_token(self, client, raw_client):
        """The integration is not a browser — it has neither of those."""
        token = generate_token(client)
        resp = post_now_playing(raw_client, token)
        assert resp.status_code == 200

    def test_regenerating_invalidates_the_previous_token(self, client):
        old = generate_token(client)
        new = generate_token(client)
        assert old != new
        assert post_now_playing(client, old).status_code == 401
        assert post_now_playing(client, new).status_code == 200


class TestWebhookValidation:
    def test_unknown_state_rejected(self, client):
        token = generate_token(client)
        resp = post_now_playing(client, token, state='dancing')
        assert resp.status_code == 400

    def test_missing_state_rejected(self, client):
        token = generate_token(client)
        resp = client.post('/api/now-playing',
                           data={'image': (io.BytesIO(JPEG_BYTES), 'art.jpg')},
                           headers={'Authorization': f'Bearer {token}'},
                           content_type='multipart/form-data')
        assert resp.status_code == 400

    def test_non_image_upload_rejected(self, client):
        """Extension says nothing — the magic bytes decide, as for posters."""
        token = generate_token(client)
        resp = post_now_playing(client, token, image=b'#!/bin/sh\nrm -rf /\n')
        assert resp.status_code == 400
        assert 'not a valid' in resp.get_json()['error']

    def test_update_without_an_image_is_accepted(self, client):
        """A stop notification carries no artwork."""
        token = generate_token(client)
        assert post_now_playing(client, token, state='idle', image=None).status_code == 200

    def test_metadata_is_stored(self, app, client):
        token = generate_token(client)
        post_now_playing(client, token, title='Bohemian Rhapsody', artist='Queen',
                         album='A Night at the Opera', entity_id='media_player.lounge')
        with app.app_context():
            np = db.session.get(NowPlaying, 1)
            assert np.state == 'playing'
            assert np.title == 'Bohemian Rhapsody'
            assert np.artist == 'Queen'
            assert np.album == 'A Night at the Opera'
            assert np.entity_id == 'media_player.lounge'
            assert np.image_filename == 'now_playing/current.jpg'

    def test_png_art_replaces_the_previous_jpeg_file(self, app, client):
        """A format change must not leave the old file behind to be served."""
        import os

        import main as main_module
        token = generate_token(client)
        post_now_playing(client, token, image=JPEG_BYTES, filename='art.jpg')
        post_now_playing(client, token, image=PNG_BYTES, filename='art.png')
        with app.app_context():
            assert db.session.get(NowPlaying, 1).image_filename == 'now_playing/current.png'
        assert not os.path.exists(os.path.join(main_module.NOWPLAYING_DIR, 'current.jpg'))


class TestFrameOverride:
    """/api/frames/<id>/next returns now-playing only when it should."""

    def _frame_with_art(self, app, client, state='playing', opted_in=True):
        upload_poster(client)
        frame_id = checkin(client)['frame_id']
        set_now_playing_flag(app, frame_id, opted_in)
        token = generate_token(client)
        post_now_playing(client, token, state=state, title='Under Pressure', artist='Queen')
        return frame_id

    def test_opted_out_frame_gets_normal_content(self, app, client):
        frame_id = self._frame_with_art(app, client, opted_in=False)
        body = client.get(f'/api/frames/{frame_id}/next').get_json()
        assert body['type'] == 'poster'

    def test_opted_in_frame_gets_now_playing(self, app, client):
        frame_id = self._frame_with_art(app, client)
        body = client.get(f'/api/frames/{frame_id}/next').get_json()
        assert body['type'] == 'now_playing'
        assert body['state'] == 'playing'
        assert body['url'].startswith('/images/now_playing/current.jpg')
        assert body['title'] == 'Under Pressure'
        assert body['artist'] == 'Queen'

    def test_paused_still_overrides_and_reports_the_state(self, app, client):
        """Pause keeps the art up — the client draws the mask over it."""
        frame_id = self._frame_with_art(app, client, state='paused')
        body = client.get(f'/api/frames/{frame_id}/next').get_json()
        assert body['type'] == 'now_playing'
        assert body['state'] == 'paused'

    def test_idle_falls_through_to_rotation(self, app, client):
        frame_id = self._frame_with_art(app, client, state='idle')
        assert client.get(f'/api/frames/{frame_id}/next').get_json()['type'] == 'poster'

    def test_off_falls_through_to_rotation(self, app, client):
        frame_id = self._frame_with_art(app, client, state='off')
        assert client.get(f'/api/frames/{frame_id}/next').get_json()['type'] == 'poster'

    def test_stale_update_falls_through_to_rotation(self, app, client):
        """An integration that dies mid-track must not wedge the frame."""
        frame_id = self._frame_with_art(app, client)
        backdate(app, 10_000)
        assert client.get(f'/api/frames/{frame_id}/next').get_json()['type'] == 'poster'

    def test_staleness_honours_the_configured_timeout(self, app, client):
        frame_id = self._frame_with_art(app, client)
        backdate(app, 300)
        assert client.get(f'/api/frames/{frame_id}/next').get_json()['type'] == 'poster'
        client.patch('/api/settings', json={'now_playing_stale_seconds': 600},
                     content_type='application/json')
        assert client.get(f'/api/frames/{frame_id}/next').get_json()['type'] == 'now_playing'

    def test_title_with_no_artwork_still_overrides_with_a_null_url(self, app, client):
        """YouTube on an Apple TV publishes no art. The title still belongs up.

        The frame draws its own placeholder for a null url, so overriding here
        is what stops the *previous* track's cover sitting under a new title.
        """
        upload_poster(client)
        frame_id = checkin(client)['frame_id']
        set_now_playing_flag(app, frame_id)
        token = generate_token(client)
        post_now_playing(client, token, state='playing', image=None,
                         title='Some Video', artwork='none')
        body = client.get(f'/api/frames/{frame_id}/next').get_json()
        assert body['type'] == 'now_playing'
        assert body['title'] == 'Some Video'
        assert body['url'] is None

    def test_nothing_to_show_at_all_falls_through_to_rotation(self, app, client):
        """No art and no title is not worth taking a frame over."""
        upload_poster(client)
        frame_id = checkin(client)['frame_id']
        set_now_playing_flag(app, frame_id)
        token = generate_token(client)
        post_now_playing(client, token, state='playing', image=None)
        assert client.get(f'/api/frames/{frame_id}/next').get_json()['type'] == 'poster'


    def test_turning_the_toggle_off_reverts_on_the_next_poll(self, app, client):
        frame_id = self._frame_with_art(app, client)
        assert client.get(f'/api/frames/{frame_id}/next').get_json()['type'] == 'now_playing'
        set_now_playing_flag(app, frame_id, False)
        assert client.get(f'/api/frames/{frame_id}/next').get_json()['type'] == 'poster'

    def test_no_content_at_all_still_reports_empty(self, app, client):
        """Falling through with an empty library is the existing behaviour."""
        frame_id = checkin(client)['frame_id']
        set_now_playing_flag(app, frame_id)
        token = generate_token(client)
        post_now_playing(client, token, state='idle')
        assert client.get(f'/api/frames/{frame_id}/next').get_json()['type'] == 'empty'

    def test_override_does_not_write_an_activity_log_row(self, app, client):
        """Album art is not library content and should not pollute history."""
        from models import FrameLog
        frame_id = self._frame_with_art(app, client)
        client.get(f'/api/frames/{frame_id}/next')
        with app.app_context():
            assert FrameLog.query.filter_by(frame_id=frame_id).count() == 0


class TestArtworkClearing:
    """The explicit ``artwork=none`` signal, and what must *not* trigger it."""

    def _opted_in_frame_with_art(self, app, client):
        upload_poster(client)
        frame_id = checkin(client)['frame_id']
        set_now_playing_flag(app, frame_id)
        token = generate_token(client)
        post_now_playing(client, token, state='playing', title='Under Pressure')
        return frame_id, token

    def test_artwork_none_clears_the_stored_art(self, app, client):
        frame_id, token = self._opted_in_frame_with_art(app, client)
        post_now_playing(client, token, state='playing', image=None,
                         title='Some Video', artwork='none')
        with app.app_context():
            assert db.session.get(NowPlaying, 1).image_filename is None
        body = client.get(f'/api/frames/{frame_id}/next').get_json()
        assert body['url'] is None

    def test_artwork_none_removes_the_file_from_disk(self, app, client):
        import os

        import main as main_module
        _frame_id, token = self._opted_in_frame_with_art(app, client)
        assert os.path.exists(os.path.join(main_module.NOWPLAYING_DIR, 'current.jpg'))
        post_now_playing(client, token, state='playing', image=None,
                         title='Some Video', artwork='none')
        assert not os.path.exists(os.path.join(main_module.NOWPLAYING_DIR, 'current.jpg'))

    def test_a_repost_without_the_signal_keeps_existing_art(self, app, client):
        """The regression this whole field exists to avoid.

        A heartbeat or a pause repost of the same track carries no image — the
        server already holds it — and no ``artwork`` field. If that were read
        as "no art", the cover would blink off every heartbeat interval.
        """
        frame_id, token = self._opted_in_frame_with_art(app, client)
        for state in ('playing', 'paused', 'playing'):
            post_now_playing(client, token, state=state, image=None,
                             title='Under Pressure')
            with app.app_context():
                assert db.session.get(NowPlaying, 1).image_filename == \
                    'now_playing/current.jpg'
            body = client.get(f'/api/frames/{frame_id}/next').get_json()
            assert body['type'] == 'now_playing'
            assert body['url'].startswith('/images/now_playing/current.jpg')

    def test_new_art_after_a_clear_is_stored_again(self, app, client):
        _frame_id, token = self._opted_in_frame_with_art(app, client)
        post_now_playing(client, token, state='playing', image=None,
                         title='Some Video', artwork='none')
        post_now_playing(client, token, state='playing', title='Back With Art')
        with app.app_context():
            assert db.session.get(NowPlaying, 1).image_filename == \
                'now_playing/current.jpg'

    def test_an_image_wins_over_a_stray_artwork_none(self, app, client):
        """Bytes on the wire are not ambiguous; never discard them."""
        _frame_id, token = self._opted_in_frame_with_art(app, client)
        post_now_playing(client, token, state='playing', title='Still Here',
                         artwork='none')
        with app.app_context():
            assert db.session.get(NowPlaying, 1).image_filename == \
                'now_playing/current.jpg'

    def test_unknown_artwork_value_is_rejected(self, client):
        token = generate_token(client)
        resp = post_now_playing(client, token, state='playing', image=None,
                                artwork='maybe')
        assert resp.status_code == 400

class TestFanOut:
    def test_only_opted_in_frames_are_signalled(self, app, client):
        opted_in = checkin(client, hostname='lounge')['frame_id']
        with app.app_context():
            other = Frame(ip='10.9.9.9', name='hallway')
            db.session.add(other)
            db.session.commit()
            other_id = other.id
        set_now_playing_flag(app, opted_in)

        token = generate_token(client)
        resp = post_now_playing(client, token)
        assert resp.get_json()['frames_signalled'] == 1
        with app.app_context():
            assert db.session.get(Frame, opted_in).pending_command == 'next'
            assert db.session.get(Frame, other_id).pending_command is None

    def test_stop_also_signals_so_reverting_is_immediate(self, app, client):
        frame_id = checkin(client)['frame_id']
        set_now_playing_flag(app, frame_id)
        token = generate_token(client)
        post_now_playing(client, token)
        # Consume the command the way the frame's signal poll does.
        client.get(f'/api/frames/{frame_id}/signal')
        post_now_playing(client, token, state='idle', image=None)
        with app.app_context():
            assert db.session.get(Frame, frame_id).pending_command == 'next'


class TestTokenAndSettings:
    def test_token_endpoint_requires_an_admin_session(self, client, raw_client):
        assert raw_client.post('/api/settings/now-playing-token').status_code == 401

    def test_raw_token_is_never_returned_by_the_settings_api(self, client):
        token = generate_token(client)
        body = client.get('/api/settings').get_json()
        assert token not in str(body)
        assert 'now_playing_webhook_token' not in body

    def test_settings_reports_only_whether_a_token_exists(self, client):
        assert client.get('/api/settings').get_json()['now_playing_token_set'] is False
        generate_token(client)
        assert client.get('/api/settings').get_json()['now_playing_token_set'] is True

    def test_stale_seconds_defaults_and_patches(self, client):
        assert client.get('/api/settings').get_json()['now_playing_stale_seconds'] == 120
        resp = client.patch('/api/settings', json={'now_playing_stale_seconds': 45},
                            content_type='application/json')
        assert resp.get_json()['now_playing_stale_seconds'] == 45

    def test_stale_seconds_is_clamped_not_rejected(self, client):
        """Matches every other numeric settings field's behaviour."""
        resp = client.patch('/api/settings', json={'now_playing_stale_seconds': 99999},
                            content_type='application/json')
        assert resp.status_code == 200
        assert resp.get_json()['now_playing_stale_seconds'] == 3600

    def test_generated_token_is_persisted(self, app, client):
        token = generate_token(client)
        with app.app_context():
            assert db.session.get(Settings, 1).now_playing_webhook_token == token


class TestFrameToggleAPI:
    def test_defaults_to_off(self, client):
        frame_id = checkin(client)['frame_id']
        assert client.get(f'/api/frames/{frame_id}').get_json()['show_now_playing'] is False

    def test_patch_turns_it_on_and_off(self, client):
        frame_id = checkin(client)['frame_id']
        resp = client.patch(f'/api/frames/{frame_id}', json={'show_now_playing': True},
                            content_type='application/json')
        assert resp.get_json()['show_now_playing'] is True
        resp = client.patch(f'/api/frames/{frame_id}', json={'show_now_playing': False},
                            content_type='application/json')
        assert resp.get_json()['show_now_playing'] is False

    def test_patch_rejects_a_non_boolean(self, client):
        frame_id = checkin(client)['frame_id']
        resp = client.patch(f'/api/frames/{frame_id}', json={'show_now_playing': []},
                            content_type='application/json')
        assert resp.status_code == 400

    def test_other_fields_are_untouched_by_the_toggle(self, client):
        frame_id = checkin(client)['frame_id']
        client.patch(f'/api/frames/{frame_id}', json={'name': 'Lounge'},
                     content_type='application/json')
        client.patch(f'/api/frames/{frame_id}', json={'show_now_playing': True},
                     content_type='application/json')
        body = client.get(f'/api/frames/{frame_id}').get_json()
        assert body['name'] == 'Lounge'
        assert body['show_now_playing'] is True
