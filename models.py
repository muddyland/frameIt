from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def utcnow():
    """Timezone-aware UTC now, stripped of tzinfo for naive SQLite storage."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Poster(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    title_above = db.Column(db.String(255), nullable=True)
    title_below = db.Column(db.String(255), nullable=True)
    sort_order = db.Column(db.Integer, default=0)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'filename': self.filename,
            'url': f'/images/{self.filename}',
            'title_above': self.title_above,
            'title_below': self.title_below,
            'sort_order': self.sort_order,
            'active': self.active,
            'created_at': self.created_at.isoformat(),
        }


class Trailer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    youtube_id = db.Column(db.String(11), nullable=False, unique=True)
    title = db.Column(db.String(255), nullable=False)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    cache_status = db.Column(db.String(12), nullable=True)    # pending/downloading/ready/error
    cached_filename = db.Column(db.String(24), nullable=True) # '{youtube_id}.mp4' when ready
    # Download retry bookkeeping — an 'error' status is retried with backoff
    # rather than being permanently sticky.
    download_attempts = db.Column(db.Integer, nullable=False, default=0)
    last_attempt_at = db.Column(db.DateTime, nullable=True)
    last_error = db.Column(db.String(255), nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'youtube_id': self.youtube_id,
            'title': self.title,
            'active': self.active,
            'created_at': self.created_at.isoformat(),
            'cache_status': self.cache_status,
            'cached_url': f'/videos/{self.cached_filename}' if self.cached_filename else None,
            'thumb_url': (f'/videos/{self.youtube_id}.jpg'
                          if self.cache_status == 'ready'
                          else f'https://img.youtube.com/vi/{self.youtube_id}/mqdefault.jpg'),
            'download_attempts': self.download_attempts or 0,
            'last_error': self.last_error,
        }


class Frame(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ip = db.Column(db.String(45), nullable=False, unique=True)
    name = db.Column(db.String(255), nullable=True)
    last_seen = db.Column(db.DateTime, nullable=True)
    rotation = db.Column(db.Integer, default=0)           # 0, 90, 180, 270
    interval_seconds = db.Column(db.Integer, default=300)
    content_mode = db.Column(db.String(10), default='pool')  # 'pool' or 'pinned'
    pinned_type = db.Column(db.String(10), nullable=True)    # 'poster' or 'trailer'
    pinned_id = db.Column(db.Integer, nullable=True)
    # Agent fields
    agent_url = db.Column(db.String(255), nullable=True)
    # Legacy credential: the registration token, reused as the agent bearer.
    # Kept for agents that have not yet updated. New registrations issue an
    # agent_secret instead and leave this untouched.
    agent_token = db.Column(db.String(64), nullable=True)
    # Per-frame credential issued at registration, independent of the
    # one-time registration token. Preferred over agent_token when set.
    agent_secret = db.Column(db.String(64), nullable=True)
    agent_last_seen = db.Column(db.DateTime, nullable=True)
    agent_version = db.Column(db.String(12), nullable=True)
    pending_command = db.Column(db.String(20), nullable=True)
    logs = db.relationship('FrameLog', backref='frame', lazy=True)

    def credential(self):
        """The bearer token this frame's agent currently accepts."""
        return self.agent_secret or self.agent_token

    def to_dict(self):
        return {
            'id': self.id,
            'ip': self.ip,
            'name': self.name or self.ip,
            'last_seen': self.last_seen.isoformat() if self.last_seen else None,
            'rotation': self.rotation,
            'interval_seconds': self.interval_seconds,
            'content_mode': self.content_mode,
            'pinned_type': self.pinned_type,
            'pinned_id': self.pinned_id,
            'agent_url': self.agent_url,
            'agent_last_seen': self.agent_last_seen.isoformat() if self.agent_last_seen else None,
            'agent_version': self.agent_version,
            'agent_auth': 'secret' if self.agent_secret else ('legacy' if self.agent_token else None),
        }


class FrameLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    frame_id = db.Column(db.Integer, db.ForeignKey('frame.id'), nullable=False)
    content_type = db.Column(db.String(10), nullable=False)  # 'poster' or 'trailer'
    content_id = db.Column(db.Integer, nullable=False)
    shown_at = db.Column(db.DateTime, default=utcnow)

    # The hot path is "most recent log row for this frame" — without this the
    # lookup is a full scan over a table that gains a row per frame per interval.
    __table_args__ = (
        db.Index('ix_framelog_frame_shown', 'frame_id', 'shown_at'),
    )


class RegistrationToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), nullable=False, unique=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    used_at = db.Column(db.DateTime, nullable=True)
    frame_id = db.Column(db.Integer, db.ForeignKey('frame.id'), nullable=True)


class AdminUser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), nullable=False, unique=True)
    password_hash = db.Column(db.String(256), nullable=False)


class Settings(db.Model):
    """Singleton settings row — always id=1."""
    id = db.Column(db.Integer, primary_key=True)
    default_title_above = db.Column(db.String(255), nullable=True, default='Now Playing')
    default_title_below = db.Column(db.String(255), nullable=True, default='')
    default_interval_seconds = db.Column(db.Integer, nullable=False, default=300)
    default_rotation = db.Column(db.Integer, nullable=False, default=0)
    default_content_mode = db.Column(db.String(10), nullable=False, default='pool')
    default_pinned_type = db.Column(db.String(10), nullable=True)   # 'poster' or 'trailer'
    default_pinned_id = db.Column(db.Integer, nullable=True)
    # Pool behaviour
    pool_order = db.Column(db.String(10), nullable=False, default='random')      # 'random' or 'sequential'
    trailer_weight_percent = db.Column(db.Integer, nullable=True)                # null = count-based
    # Dashboard
    dashboard_refresh_seconds = db.Column(db.Integer, nullable=False, default=30)
    # How often a frame asks whether a command is waiting. This is what decides
    # how quickly a change made elsewhere — pinning now-playing artwork, say —
    # actually reaches the screen, so it is deliberately short. Raise it if a
    # large fleet makes the request rate a problem.
    signal_poll_seconds = db.Column(db.Integer, nullable=False, default=1)
    # Maintenance
    log_retention_days = db.Column(db.Integer, nullable=True)                    # null = keep forever
    # Cycling poster text options (newline-separated; overrides single-value fields when set)
    default_title_above_options = db.Column(db.Text, nullable=True)
    default_title_below_options = db.Column(db.Text, nullable=True)
    # Security posture. All three default to the permissive setting on an
    # existing database so an upgrade never orphans a running frame; flip them
    # on once every agent and kiosk has picked up the new code.
    strict_agent_auth = db.Column(db.Boolean, nullable=False, default=False)
    strict_frame_auth = db.Column(db.Boolean, nullable=False, default=False)
    allow_bypass_frames = db.Column(db.Boolean, nullable=False, default=False)

    def to_dict(self):
        return {
            'default_title_above': self.default_title_above or '',
            'default_title_below': self.default_title_below or '',
            'default_interval_seconds': self.default_interval_seconds or 300,
            'default_rotation': self.default_rotation if self.default_rotation is not None else 0,
            'default_content_mode': self.default_content_mode or 'pool',
            'default_pinned_type': self.default_pinned_type or '',
            'default_pinned_id': self.default_pinned_id,
            'pool_order': self.pool_order or 'random',
            'trailer_weight_percent': self.trailer_weight_percent,
            'dashboard_refresh_seconds': self.dashboard_refresh_seconds or 30,
            'signal_poll_seconds': self.signal_poll_seconds or 1,
            'log_retention_days': self.log_retention_days,
            'default_title_above_options': self.default_title_above_options or '',
            'default_title_below_options': self.default_title_below_options or '',
            'strict_agent_auth': bool(self.strict_agent_auth),
            'strict_frame_auth': bool(self.strict_frame_auth),
            'allow_bypass_frames': bool(self.allow_bypass_frames),
        }
