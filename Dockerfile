# Matches the Python used by CI so the tested combination is the shipped one.
FROM python:3.12-slim

# ffmpeg lets the trailer cache merge separate video+audio streams for 720p.
# Without it yt-dlp falls back to pre-merged streams, usually 480p.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /frameit

# Dependencies first so application edits don't invalidate the layer.
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY . .

# Run unprivileged.
#
# DATA_DIR / IMAGES_DIR / VIDEOS_DIR are deliberately NOT set here. The app
# defaults to ./config, ./images and ./videos relative to the working
# directory, which is where existing containers keep their data — setting them
# would silently point an upgraded container at an empty database. The
# root-level paths the previous image created are kept writable too, for
# deployments that set those variables themselves.
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin frameit \
    && mkdir -p /frameit/config /frameit/images /frameit/videos /data /config /images /videos \
    && chown -R frameit:frameit /frameit /data /config /images /videos

ENV PYTHONUNBUFFERED=1

VOLUME ["/frameit/config", "/frameit/images", "/frameit/videos"]

USER frameit
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:5000/healthz || exit 1

# Threaded workers: a proxied `apt upgrade` streams for minutes and would
# otherwise hold a sync worker, stalling every frame for the duration.
CMD ["python", "-m", "gunicorn", \
     "-k", "gthread", "-w", "2", "--threads", "8", \
     "--timeout", "600", "--graceful-timeout", "30", \
     "-b", ":5000", "main:app"]
