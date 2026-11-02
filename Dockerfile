# The Python images for the build stage and the runtime stage. Both default to
# python:3.12-slim, which matches the Python used by CI so the tested
# combination is the shipped one. To build on a hardened, shell-less runtime
# instead, set them to a matching Docker Hardened Images pair: the "-dev"
# variant with a shell, pip and apt to build in, and its runtime sibling to
# ship. CI's USE_DHI=true does this.
#
#   --build-arg PYTHON_BUILDER_IMAGE=dhi/python:3.12-debian13-dev
#   --build-arg PYTHON_RUNTIME_IMAGE=dhi/python:3.12-debian13
#
# Both must use the same libc, and debian13 rather than alpine: ffmpeg comes
# from Debian's packages and is copied across as glibc binaries, the same as
# on slim.
ARG PYTHON_BUILDER_IMAGE=python:3.12-slim
ARG PYTHON_RUNTIME_IMAGE=python:3.12-slim

# Only read from: the build stage compares ffmpeg's libraries against what the
# runtime image already ships.
FROM ${PYTHON_RUNTIME_IMAGE} AS runtime-base

# Stage 1: everything that needs a shell.
FROM ${PYTHON_BUILDER_IMAGE} AS build
# Package source for pip. Unset, pip uses PyPI; CI passes the internal mirror
# through when one is configured. Build-time only: not baked into the image.
ARG PIP_INDEX_URL
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# ffmpeg lets the trailer cache merge separate video+audio streams for 720p.
# Without it yt-dlp falls back to pre-merged streams, usually 480p.
#
# The runtime stage can't apt-get (the hardened image has no shell or package
# manager), so ffmpeg is installed here and copied across: the two binaries,
# plus every shared library they load that the runtime image doesn't already
# have, each at its original path so nothing needs LD_LIBRARY_PATH or an
# ldconfig run. Libraries the runtime already ships (glibc, zlib, ...) are
# left alone, so the runtime keeps its own builds of them.
#
# init-system-helpers goes in first: x11-common (pulled in through SDL2) calls
# update-rc.d from its postinst, and the DHI -dev image doesn't ship it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends init-system-helpers \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=runtime-base /usr/lib /runtime/usr/lib
RUN set -eu; \
    if ldd /usr/bin/ffmpeg /usr/bin/ffprobe | grep 'not found'; then exit 1; fi; \
    mkdir -p /skel/ffmpeg/usr/bin; \
    cp /usr/bin/ffmpeg /usr/bin/ffprobe /skel/ffmpeg/usr/bin/; \
    ldd /usr/bin/ffmpeg /usr/bin/ffprobe \
    | awk '$2 == "=>" && $3 ~ /^\// {print $3}' | sort -u \
    | while read -r lib; do \
        dir=$(realpath "$(dirname "$lib")"); name=$(basename "$lib"); \
        if [ -e "/runtime$dir/$name" ]; then continue; fi; \
        mkdir -p "/skel/ffmpeg$dir"; \
        cp -L "$lib" "/skel/ffmpeg$dir/$name"; \
    done; \
    rm -rf /runtime

# --target into a plain directory rather than the interpreter's site-packages:
# the runtime stage may be a different image (DHI keeps its interpreter under
# /usr, slim under /usr/local), and a directory on PYTHONPATH works on either.
COPY requirements.lock /tmp/requirements.lock
RUN pip install --target /opt/pydeps -r /tmp/requirements.lock

# An empty directory for the runtime stage to copy into place with the right
# owner, since it has no mkdir or chown to run.
RUN mkdir -p /skel/empty

# Stage 2: runtime.
#
# No RUN steps: the hardened runtime has no shell, so everything here is a
# COPY or metadata and the stage builds the same way on either base.
FROM ${PYTHON_RUNTIME_IMAGE}

WORKDIR /frameit

# HOME: uid 10001 has no passwd entry or home directory, and the root
# filesystem may be read-only. gunicorn puts its control socket under
# $HOME/.gunicorn and yt-dlp its cache under $HOME/.cache, so point HOME at
# /tmp, which a hardened deployment mounts as a tmpfs.
ENV PYTHONPATH=/opt/pydeps \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp

COPY --from=build /skel/ffmpeg/ /
# Dependencies before the app so application edits don't invalidate the layer.
COPY --from=build /opt/pydeps /opt/pydeps

# Root-owned, so the process can't rewrite its own code.
COPY . .
# main.py creates static/dist at import. It is made here (empty, root-owned)
# so that succeeds without the code directory being writable.
COPY --from=build /skel/empty /frameit/static/dist

# Run unprivileged, as uid 10001 on both bases: existing volumes already
# belong to it. There is no passwd entry for it, and nothing looks one up.
#
# DATA_DIR / IMAGES_DIR / VIDEOS_DIR are deliberately NOT set here. The app
# defaults to ./config, ./images and ./videos relative to the working
# directory, which is where existing containers keep their data — setting them
# would silently point an upgraded container at an empty database. The
# root-level paths the previous image created are kept writable too, for
# deployments that set those variables themselves.
COPY --from=build --chown=10001:10001 /skel/empty /frameit/config
COPY --from=build --chown=10001:10001 /skel/empty /frameit/images
COPY --from=build --chown=10001:10001 /skel/empty /frameit/videos
COPY --from=build --chown=10001:10001 /skel/empty /data
COPY --from=build --chown=10001:10001 /skel/empty /config
COPY --from=build --chown=10001:10001 /skel/empty /images
COPY --from=build --chown=10001:10001 /skel/empty /videos

VOLUME ["/frameit/config", "/frameit/images", "/frameit/videos"]

USER 10001:10001
EXPOSE 5000

# Exec form, since the hardened image has no shell (or curl).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=4)"]

# Threaded workers: a proxied `apt upgrade` streams for minutes and would
# otherwise hold a sync worker, stalling every frame for the duration.
# `python -m`: gunicorn's launcher script is in /opt/pydeps/bin, not on PATH.
CMD ["python", "-m", "gunicorn", \
     "-k", "gthread", "-w", "2", "--threads", "8", \
     "--timeout", "600", "--graceful-timeout", "30", \
     "-b", ":5000", "main:app"]
