# memriver e2e verification image.
#
# Purpose: run the PENDING live-harness rows of docs/verification/phase2-hooks.md
# in an isolated container, driving the literal commands the installer writes
# (uvx memriver ...) against locally-built wheels instead of PyPI (memriver is
# not published there).
#
# Root user: this image only ever runs stage1.sh/stage2.sh/stage5.sh, never an
# interactive `claude`/`codex` session, so none of Claude Code's --dangerously-*
# flags (which it refuses to honour as root) are needed. Codex CLI has no such
# root restriction. Root is acceptable for these stages; a non-root user would
# only matter for an interactive session, which stays a manual step (see
# e2e/README.md).
FROM python:3.12-slim

# Static uv/uvx binaries -- no pip bootstrap, no network install step.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# curl + nodesource's Node 22, with a plain-Debian nodejs/npm fallback if the
# nodesource script or its apt repo is flaky. Either way `claude --version`
# must work afterwards -- Claude Code only needs node >=18.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg; \
    if curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
        && apt-get install -y --no-install-recommends nodejs; then \
        echo "nodesource nodejs installed"; \
    else \
        echo "nodesource install failed, falling back to Debian nodejs/npm"; \
        apt-get install -y --no-install-recommends nodejs npm; \
    fi; \
    npm install -g @anthropic-ai/claude-code @openai/codex; \
    apt-get purge -y gnupg; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*

# Wheels for memriver + memriver-core are bind-mounted read-only at /wheels at
# runtime (see run-stage1.sh / run-stage2.sh). This makes every literal
# `uvx memriver ...` command the installer writes -- and `uv run --with
# memriver` -- resolve locally with no package index involved.
ENV UV_FIND_LINKS=/wheels
# uv defaults to hardlinking from its cache; the wheel mount and uv's cache can
# sit on different filesystems inside Docker, which turns that into a (harmless
# but noisy) warning on every run. Copying instead silences it.
ENV UV_LINK_MODE=copy

# NEVER put credentials (CLAUDE_CODE_OAUTH_TOKEN, ~/.codex/auth.json, or
# anything else) here -- stage2/stage3's token is passed at `docker run` time
# only, by name; stage5's Codex auth file is bind-mounted read-only at
# /host-auth.json and copied into the container's own throwaway HOME.

WORKDIR /root
