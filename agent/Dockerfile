FROM node:22-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git ca-certificates curl python3 python3-venv ripgrep jq \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

RUN npm install -g @anthropic-ai/claude-code@2.1.272 \
    && npm cache clean --force

# the image ships uid 1000 as "node"; the daemon commits as uid 1000, so keep that uid
RUN usermod --login agent --home /home/agent --move-home node \
    && groupmod --new-name agent node

# the rootfs is read-only at runtime, so claude must not try to replace itself
ENV HOME=/home/agent \
    DISABLE_AUTOUPDATER=1

WORKDIR /work
USER agent
