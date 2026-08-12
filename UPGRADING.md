# Upgrading FrameIT

## Upgrading to the hardened release

This release closes a set of security and stability issues. It is designed so a
running installation can be upgraded without touching the Pis first — but there
are a few things to know, and two switches to flip once your frames have caught
up.

**Short version:** deploy the server, sign in again, run *Agent Update* on each
frame, then turn on the two strict-mode switches in **Settings → Security**.

---

### 1. Deploy the server

Nothing manual is required. On startup the app creates any missing columns and
indexes, seeds the settings row, and switches the database to WAL mode. Every
step is idempotent and safe to run from several gunicorn workers at once.

```bash
git pull
pip install -r requirements.txt
# restart your service / redeploy the container
```

Docker users: the image now runs as an unprivileged user (uid 10001). Your data
paths are unchanged — `DATA_DIR`, `IMAGES_DIR` and `VIDEOS_DIR` are deliberately
not set in the image, so whatever your compose file or defaults resolved to
before still applies.

The one thing to do is make the mount writable by the new user:

```bash
sudo chown -R 10001:10001 /path/to/your/frameit/data
```

If you mount named volumes rather than host directories, Docker copies the
image's ownership on first use and nothing is needed.

#### What the migration does

| Change | Effect on existing data |
|---|---|
| Adds `frame.agent_secret` | Null for existing frames; they keep using the old token until their agent updates |
| Adds `trailer.download_attempts`, `last_attempt_at`, `last_error` | Retry bookkeeping; defaults to 0/null |
| Adds three `settings` flags | All default to the permissive setting |
| Creates indexes on `frame_log` and `trailer` | Purely additive |
| Switches SQLite to WAL | Creates `frameit.db-wal` and `frameit.db-shm` beside the database — back up all three together |
| Tightens `secret.key` to 0600 | Permissions only; the key is **not** rotated |

Nothing is deleted, and log retention stays at whatever you had it set to. New
installations default to 30 days; existing ones are left alone.

### 2. Sign in again

Sessions now carry a fingerprint of your password hash, so changing your
password signs out every other session. Existing cookies predate that field and
are treated as stale — expect to log in once after upgrading.

Passwords now have an 8-character minimum, enforced at setup and on change.
Your current password is not re-validated, so a shorter existing one keeps
working until you change it.

### 3. Update the agents

Frames page → **Agent** → **Agent Update** on each frame. This is worth doing
promptly: the updated agent verifies the download against the hash the server
publishes before replacing itself, and requests a dedicated credential instead
of reusing the registration token.

**Settings → Security** shows which frames are still on the old credential.

### 4. Re-run the installer on each Pi (optional but recommended)

The agent update replaces `agent.py` only. To pick up the tightened sudoers
rules and the WiFi helper, re-run the installer on each Pi — it is idempotent:

```bash
curl -fsSL http://your-server:5000/install.sh -o frameit-install.sh
sudo bash frameit-install.sh --server http://your-server:5000 --token <token>
```

This removes the stale `hostnamectl set-hostname *` grant and replaces the
wildcard `nmcli dev wifi connect *` rule with a fixed helper that takes the
passphrase on stdin. The agent falls back to the old path when the helper is
absent, so an un-reinstalled Pi keeps working.

### 5. Turn on strict mode

Both switches live in **Settings → Security** and both default to **off**, so
the upgrade itself never orphans a device.

- **Require agent authentication** — turn on once the readiness banner is
  green. Any frame still listed will stop reporting in.
- **Require frame tokens** — reboot or send **Refresh** to every display first
  so they reload and pick up a token. A display still running the old page will
  go blank otherwise.

---

## Other behaviour changes

**Preview frames are off by default.** `?bypass_install=1` no longer creates a
frame unless *Allow preview frames* is enabled. Frames created this way in the
past are unaffected.

**Re-registration is authorised by the token, not the IP address.** A frame
whose DHCP lease changes can now re-register and the record follows it — that
previously failed permanently. Registration also rejects a non-numeric `port`
and ignores a hostname that is not a valid hostname.

**Uploads are checked and capped.** Files are verified by magic bytes, not just
extension, and capped at 16 MB (`MAX_UPLOAD_MB` to change).

**Bad request bodies return 400, not 500.** Sending the wrong JSON type now
produces a 400 naming the field. Fields that previously clamped — interval,
dashboard refresh, retention — still clamp rather than reject.

**Failed trailer downloads retry.** An `error` status used to be permanent.
Downloads now retry with exponential backoff up to 5 attempts, are capped at
600 MB (`MAX_DOWNLOAD_MB`), and refuse to start when disk is low.

**Four unused templates were deleted**: `list.html`, `trailer.html`,
`upload.html`, `admin_tokens.html`. No route rendered them.

**`flask-cors` was removed** from requirements. It was declared but never
imported.

---

## New environment variables

All optional; the defaults preserve existing behaviour.

| Variable | Default | Purpose |
|---|---|---|
| `TRUSTED_PROXY_HOPS` | `1` | Reverse-proxy hops to trust. **Set to `0` if gunicorn is exposed directly** — otherwise clients can forge `X-Forwarded-For` |
| `FORCE_HTTPS` | unset | Sets `Secure` on the session cookie and enables HSTS |
| `MAX_UPLOAD_MB` | `16` | Upload size cap |
| `MAX_DOWNLOAD_MB` | `600` | Per-video cache cap |
| `MAINTENANCE_INTERVAL_SECONDS` | `900` | Housekeeping sweep interval |
| `FRAMEIT_DISABLE_WORKERS` | unset | Skips background threads (tests, one-shot CLI) |
| `FLASK_DEBUG` / `FLASK_RUN_HOST` | off / `127.0.0.1` | `python main.py` no longer starts a debugger on all interfaces |

## Recommended gunicorn change

A proxied `apt upgrade` streams for minutes and holds a sync worker for the
duration, stalling every frame. Use threaded workers:

```bash
gunicorn -k gthread -w 2 --threads 8 --timeout 600 -b 0.0.0.0:5000 main:app
```

## Rolling back

The schema changes are additive, so the previous release runs against an
upgraded database — it ignores the new columns. Two caveats:

1. WAL mode persists. The old code handles it fine, but check-point and copy all
   three database files if you move the data.
2. Agents that have taken a dedicated credential will 401 against the old
   server, which only knows the registration token. Delete
   `/opt/frameit-agent/agent.secret` on the Pi and restart `frameit-agent` to
   fall back.
