FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

# The base image ships a yarn apt source whose signing key is not present, so
# `apt-get update` fails on it. Removing the list is load-bearing, not tidying.
RUN rm -f /etc/apt/sources.list.d/yarn.list 2>/dev/null || true

# ffmpeg: interval download cutting and frame extraction (media ladder tier 2+)
# tesseract: local OCR fallback
# deno: yt-dlp needs a JS runtime for YouTube; upstream's youtube.py check
#       prefers deno because it works without extra --js-runtimes config
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        tesseract-ocr \
        jq \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# Node — upstream's installer requires it for mcporter, which is how the
# search channel is reached. Upstream documents that channel as "auto-
# configured, free, no key", so its absence here was an installation gap, not
# a missing credential: two benchmark runs both stalled on it and wrote their
# own search fallback.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g mcporter \
    && rm -rf /var/lib/apt/lists/* \
    && node --version && mcporter --version

RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y \
    && deno --version

USER vscode

# Channel backends that must survive a rebuild. The container is disposable by
# design, so anything an agent needs has to be in the image — a tool installed
# into a running container is gone the next time it is recreated, leaving
# credentials on disk with nothing to use them.
# Pinned to the commit upstream's reddit.py pins; PyPI lags (upstream issue #10).
RUN pipx install 'git+https://github.com/public-clis/rdt-cli.git@5e4fb3720d5c174e976cd425ccc3b879d52cac66' \
    && rdt --help >/dev/null

# Dependencies come from pyproject (upstream core + the media extra), so this
# image never pins a second, divergent dependency list.
