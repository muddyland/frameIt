"""Tests for the durability, migration and housekeeping behaviour."""
from datetime import timedelta
from unittest.mock import patch

import main as main_module
from models import db, Frame, FrameLog, Poster, Settings, Trailer, utcnow
from tests.conftest import add_trailer, checkin, upload_poster


class TestSqlitePragmas:
    def test_wal_and_busy_timeout_applied(self, app):
        with app.app_context():
            mode = db.session.execute(db.text('PRAGMA journal_mode')).scalar()
            busy = db.session.execute(db.text('PRAGMA busy_timeout')).scalar()
            assert str(mode).lower() == 'wal'
            assert int(busy) >= 5000


class TestMigrations:
    def test_migrate_schema_is_idempotent(self, app):
        """Every worker runs this on boot, so it must survive being re-run."""
        with app.app_context():
            main_module.migrate_schema()
            main_module.migrate_schema()

    def test_indexes_exist(self, app):
        with app.app_context():
            rows = db.session.execute(db.text(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )).fetchall()
            names = {r[0] for r in rows}
            assert 'ix_framelog_frame_shown' in names

    def test_index_names_match_real_tables(self, app):
        """Guards the upgrade path, where create_all() adds nothing.

        A wrong table name in _INDEXES is invisible on a fresh database —
        create_all() builds the index from the model — but silently skips it
        on an existing one, which is the case that actually matters.
        """
        with app.app_context():
            real = {r[0] for r in db.session.execute(db.text(
                "SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}
            for name, table, _cols in main_module._INDEXES:
                assert table in real, f'index {name} targets unknown table {table!r}'

    def test_indexes_recreated_after_being_dropped(self, app):
        """Simulates an upgrade: the table exists, the index does not."""
        with app.app_context():
            db.session.execute(db.text('DROP INDEX IF EXISTS ix_framelog_frame_shown'))
            db.session.commit()
            main_module.migrate_schema()
            names = {r[0] for r in db.session.execute(db.text(
                "SELECT name FROM sqlite_master WHERE type='index'")).fetchall()}
            assert 'ix_framelog_frame_shown' in names

    def test_new_columns_present(self, app):
        with app.app_context():
            cols = {r[1] for r in db.session.execute(
                db.text('PRAGMA table_info("frame")')).fetchall()}
            assert 'agent_secret' in cols
            cols = {r[1] for r in db.session.execute(
                db.text('PRAGMA table_info("trailer")')).fetchall()}
            assert {'download_attempts', 'last_attempt_at', 'last_error'} <= cols

    def test_migration_skips_missing_tables(self, app):
        """A partially created database must not blow up the boot path."""
        with app.app_context():
            with db.engine.connect() as conn:
                assert main_module._table_exists(conn, 'frame') is True
                assert main_module._table_exists(conn, 'does_not_exist') is False


class TestInputCoercion:
    """Wrong JSON types return 400 with the field name, not an HTML 500."""

    def test_non_numeric_interval_returns_400(self, client):
        frame_id = checkin(client)['frame_id']
        resp = client.patch(f'/api/frames/{frame_id}', json={'interval_seconds': 'soon'})
        assert resp.status_code == 400
        assert 'interval_seconds' in resp.get_json()['error']

    def test_non_string_name_returns_400(self, client):
        frame_id = checkin(client)['frame_id']
        resp = client.patch(f'/api/frames/{frame_id}', json={'name': 42})
        assert resp.status_code == 400

    def test_non_string_poster_title_returns_400(self, client):
        poster_id = upload_poster(client).get_json()['id']
        resp = client.patch(f'/api/posters/{poster_id}', json={'title_above': ['a']})
        assert resp.status_code == 400

    def test_bad_reorder_payload_returns_400(self, client):
        poster_id = upload_poster(client).get_json()['id']
        assert client.post('/api/posters/reorder', json={'nope': 1}).status_code == 400
        assert client.post('/api/posters/reorder',
                           json=[{'id': poster_id, 'sort_order': 'x'}]).status_code == 400
        assert client.post('/api/posters/reorder',
                           json=[{'id': poster_id, 'sort_order': 3}]).status_code == 200

    def test_invalid_rotation_returns_400(self, client):
        frame_id = checkin(client)['frame_id']
        resp = client.patch(f'/api/frames/{frame_id}', json={'rotation': 45})
        assert resp.status_code == 400

    def test_interval_still_clamps(self, client):
        """Clamping was the existing contract — keep it rather than 400ing."""
        frame_id = checkin(client)['frame_id']
        resp = client.patch(f'/api/frames/{frame_id}', json={'interval_seconds': 1})
        assert resp.get_json()['interval_seconds'] == 10

    def test_settings_reject_bad_types(self, client):
        assert client.patch('/api/settings',
                            json={'trailer_weight_percent': 'lots'}).status_code == 400
        assert client.patch('/api/settings',
                            json={'trailer_weight_percent': 500}).status_code == 400

    def test_api_errors_are_json(self, client):
        resp = client.get('/api/frames/999999')
        assert resp.status_code == 404
        assert resp.is_json


class TestDownloadClaiming:
    def test_claim_is_exclusive(self, app, client):
        add_trailer(client, url='dQw4w9WgXcQ')
        with app.app_context():
            assert main_module._claim_download('dQw4w9WgXcQ') is True
            # Second worker must not get the same job.
            assert main_module._claim_download('dQw4w9WgXcQ') is False
            trailer = Trailer.query.filter_by(youtube_id='dQw4w9WgXcQ').first()
            assert trailer.cache_status == 'downloading'
            assert trailer.download_attempts == 1

    def test_claim_retries_errored_rows(self, app, client):
        add_trailer(client, url='dQw4w9WgXcQ')
        with app.app_context():
            trailer = Trailer.query.filter_by(youtube_id='dQw4w9WgXcQ').first()
            trailer.cache_status = 'error'
            db.session.commit()
            assert main_module._claim_download('dQw4w9WgXcQ') is True

    def test_claim_skips_ready_rows(self, app, client):
        add_trailer(client, url='dQw4w9WgXcQ')
        with app.app_context():
            trailer = Trailer.query.filter_by(youtube_id='dQw4w9WgXcQ').first()
            trailer.cache_status = 'ready'
            trailer.cached_filename = 'dQw4w9WgXcQ.mp4'
            db.session.commit()
            assert main_module._claim_download('dQw4w9WgXcQ') is False


class TestDownloadRetry:
    def _trailer(self, app, client, **kwargs):
        add_trailer(client, url='dQw4w9WgXcQ')
        with app.app_context():
            trailer = Trailer.query.filter_by(youtube_id='dQw4w9WgXcQ').first()
            for key, value in kwargs.items():
                setattr(trailer, key, value)
            db.session.commit()
            db.session.refresh(trailer)
            return trailer

    def test_error_is_retried_after_backoff(self, app, client):
        with app.app_context():
            self._trailer(app, client, cache_status='error', download_attempts=1,
                          last_attempt_at=utcnow() - timedelta(hours=2))
            trailer = Trailer.query.filter_by(youtube_id='dQw4w9WgXcQ').first()
            assert main_module._retry_due(trailer) is True

    def test_error_is_not_retried_immediately(self, app, client):
        with app.app_context():
            self._trailer(app, client, cache_status='error', download_attempts=1,
                          last_attempt_at=utcnow())
            trailer = Trailer.query.filter_by(youtube_id='dQw4w9WgXcQ').first()
            assert main_module._retry_due(trailer) is False

    def test_retry_gives_up_eventually(self, app, client):
        with app.app_context():
            self._trailer(app, client, cache_status='error',
                          download_attempts=main_module.MAX_DOWNLOAD_ATTEMPTS,
                          last_attempt_at=utcnow() - timedelta(days=7))
            trailer = Trailer.query.filter_by(youtube_id='dQw4w9WgXcQ').first()
            assert main_module._retry_due(trailer) is False

    def test_next_requeues_an_eligible_error(self, app, client):
        """A transient failure used to make a trailer permanently uncacheable."""
        add_trailer(client, url='dQw4w9WgXcQ')
        with app.app_context():
            trailer = Trailer.query.filter_by(youtube_id='dQw4w9WgXcQ').first()
            trailer.cache_status = 'error'
            trailer.download_attempts = 1
            trailer.last_attempt_at = utcnow() - timedelta(hours=6)
            db.session.commit()
        frame_id = checkin(client)['frame_id']
        with patch.object(main_module, '_enqueue_download') as mock_enqueue:
            client.get(f'/api/frames/{frame_id}/next')
            mock_enqueue.assert_called_once_with('dQw4w9WgXcQ')

    def test_clear_cache_resets_retry_state(self, app, client):
        trailer_id = add_trailer(client, url='dQw4w9WgXcQ').get_json()['id']
        with app.app_context():
            trailer = db.session.get(Trailer, trailer_id)
            trailer.cache_status = 'error'
            trailer.download_attempts = 4
            db.session.commit()
        client.delete(f'/api/trailers/{trailer_id}/cache')
        with app.app_context():
            trailer = db.session.get(Trailer, trailer_id)
            assert trailer.download_attempts == 0
            assert trailer.cache_status is None


class TestMaintenance:
    def test_prune_respects_retention(self, app, client):
        frame_id = checkin(client)['frame_id']
        with app.app_context():
            db.session.add(FrameLog(frame_id=frame_id, content_type='poster',
                                    content_id=1, shown_at=utcnow() - timedelta(days=90)))
            db.session.add(FrameLog(frame_id=frame_id, content_type='poster',
                                    content_id=2, shown_at=utcnow()))
            settings = db.session.get(Settings, 1)
            settings.log_retention_days = 30
            db.session.commit()

            assert main_module.prune_frame_logs() == 1
            assert FrameLog.query.count() == 1

    def test_prune_is_a_noop_when_retention_unset(self, app, client):
        """An upgrade must not start deleting history nobody asked it to."""
        frame_id = checkin(client)['frame_id']
        with app.app_context():
            db.session.add(FrameLog(frame_id=frame_id, content_type='poster',
                                    content_id=1, shown_at=utcnow() - timedelta(days=900)))
            settings = db.session.get(Settings, 1)
            settings.log_retention_days = None
            db.session.commit()

            assert main_module.prune_frame_logs() == 0
            assert FrameLog.query.count() == 1

    def test_next_no_longer_prunes_inline(self, app, client):
        """Pruning moved to the sweep; the request path does one insert."""
        upload_poster(client)
        frame_id = checkin(client)['frame_id']
        with app.app_context():
            db.session.add(FrameLog(frame_id=frame_id, content_type='poster',
                                    content_id=1, shown_at=utcnow() - timedelta(days=900)))
            settings = db.session.get(Settings, 1)
            settings.log_retention_days = 30
            db.session.commit()

        client.get(f'/api/frames/{frame_id}/next')

        with app.app_context():
            old = FrameLog.query.filter(
                FrameLog.shown_at < utcnow() - timedelta(days=100)).count()
            assert old == 1, 'the request path should not be deleting rows'

    def test_orphan_sweep_removes_unreferenced_media(self, app, client):
        import os
        videos = os.environ['VIDEOS_DIR']
        keep_id = 'dQw4w9WgXcQ'
        add_trailer(client, url=keep_id)
        open(os.path.join(videos, f'{keep_id}.mp4'), 'wb').close()
        open(os.path.join(videos, 'orphaned11.mp4'), 'wb').close()
        open(os.path.join(videos, 'notmedia.txt'), 'wb').close()

        with app.app_context():
            removed = main_module.sweep_orphan_videos()

        assert removed == 1
        assert os.path.exists(os.path.join(videos, f'{keep_id}.mp4'))
        assert not os.path.exists(os.path.join(videos, 'orphaned11.mp4'))
        assert os.path.exists(os.path.join(videos, 'notmedia.txt'))
        os.remove(os.path.join(videos, f'{keep_id}.mp4'))
        os.remove(os.path.join(videos, 'notmedia.txt'))

    def test_stalled_download_is_reset(self, app, client):
        add_trailer(client, url='dQw4w9WgXcQ')
        with app.app_context():
            trailer = Trailer.query.filter_by(youtube_id='dQw4w9WgXcQ').first()
            trailer.cache_status = 'downloading'
            trailer.last_attempt_at = utcnow() - timedelta(hours=12)
            db.session.commit()

            with patch.object(main_module, '_enqueue_download'):
                main_module.requeue_pending_downloads()

            trailer = Trailer.query.filter_by(youtube_id='dQw4w9WgXcQ').first()
            assert trailer.cache_status == 'error'

    def test_fresh_download_is_left_alone(self, app, client):
        add_trailer(client, url='dQw4w9WgXcQ')
        with app.app_context():
            trailer = Trailer.query.filter_by(youtube_id='dQw4w9WgXcQ').first()
            trailer.cache_status = 'downloading'
            trailer.last_attempt_at = utcnow()
            db.session.commit()

            with patch.object(main_module, '_enqueue_download'):
                main_module.requeue_pending_downloads()

            trailer = Trailer.query.filter_by(youtube_id='dQw4w9WgXcQ').first()
            assert trailer.cache_status == 'downloading'


class TestDuplicateAdvance:
    def test_rapid_repeat_writes_one_log_row(self, app, client):
        """A kiosk hitting an unplayable video fires two fetches at once."""
        upload_poster(client)
        frame_id = checkin(client)['frame_id']
        client.get(f'/api/frames/{frame_id}/next')
        client.get(f'/api/frames/{frame_id}/next')
        with app.app_context():
            assert FrameLog.query.filter_by(frame_id=frame_id).count() == 1


class TestDisplayState:
    def test_no_agent_reports_on(self, client):
        frame_id = checkin(client)['frame_id']
        resp = client.get(f'/api/frames/{frame_id}/display-state')
        assert resp.status_code == 200
        assert resp.get_json() == {'on': True, 'agent': False}

    def test_unreachable_agent_reports_on(self, client):
        """Failing open keeps a frame showing content if the agent is down."""
        token = client.post('/api/tokens').get_json()['token']
        frame_id = client.post('/api/agents/register',
                               json={'token': token, 'hostname': 'pi', 'port': 5001},
                               ).get_json()['frame_id']
        body = client.get(f'/api/frames/{frame_id}/display-state').get_json()
        assert body['on'] is True


class TestHealthz:
    def test_reports_ok(self, raw_client):
        resp = raw_client.get('/healthz')
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['ok'] is True
        assert 'agent_version' in body

    def test_needs_no_authentication(self, raw_client):
        assert raw_client.get('/healthz').status_code == 200


class TestQueryBatching:
    """Preview resolution must not scale with the number of frames."""

    @staticmethod
    def _count_queries(app, fn):
        from sqlalchemy import event
        from sqlalchemy.engine import Engine

        seen = []

        def before(_conn, _cur, statement, *_a, **_kw):
            if statement.lstrip().upper().startswith('SELECT'):
                seen.append(statement)

        event.listen(Engine, 'before_cursor_execute', before)
        try:
            result = fn()
        finally:
            event.remove(Engine, 'before_cursor_execute', before)
        return result, len(seen)

    def _seed(self, app, client, count):
        upload_poster(client)
        with app.app_context():
            poster_id = Poster.query.first().id
            for i in range(count):
                frame = Frame(ip=f'10.0.0.{i}', name=f'frame{i}')
                db.session.add(frame)
                db.session.flush()
                db.session.add(FrameLog(frame_id=frame.id, content_type='poster',
                                        content_id=poster_id))
            db.session.commit()

    def test_frame_listing_query_count_is_flat(self, app, client):
        self._seed(app, client, 3)
        resp_a, few = self._count_queries(app, lambda: client.get('/api/frames'))
        assert resp_a.status_code == 200
        assert len(resp_a.get_json()) == 3
        assert all(f['preview'] is not None for f in resp_a.get_json())

        with app.app_context():
            # Logs first — foreign keys are enforced now.
            FrameLog.query.delete()
            Frame.query.delete()
            db.session.commit()

        self._seed(app, client, 12)
        resp_b, many = self._count_queries(app, lambda: client.get('/api/frames'))
        assert len(resp_b.get_json()) == 12

        # Four times the frames must not mean four times the queries. The old
        # implementation ran two SELECTs per frame.
        assert many <= few + 2, f'query count grew with frame count: {few} -> {many}'

    def test_dashboard_summary_query_count_is_flat(self, app, client):
        self._seed(app, client, 10)
        resp, count = self._count_queries(app, lambda: client.get('/api/dashboard/summary'))
        assert resp.status_code == 200
        activity = resp.get_json()['activity']
        assert activity and all(a['title'] for a in activity)
        # 6 aggregate counts + logs + frames + posters + trailers + settings.
        assert count <= 16, f'dashboard ran {count} SELECTs'
