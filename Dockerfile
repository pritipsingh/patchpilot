# ===========================================================================
# PatchPilot
# ===========================================================================
FROM agnohq/python:3.12

# git is a runtime dependency: the agent works in real checkouts.
# Node comes with it because the allowlist covers TypeScript repositories -- an
# agent that cannot run `pnpm test` cannot verify a change it made to one.
# Drop this block if every repo you allow is Python.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && corepack enable \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

COPY requirements.txt ./
# `pip sync` installs exactly what is listed and resolves no dependencies, so the
# extras (PyGithub, slack_sdk) silently never arrive. `pip install` resolves them.
RUN uv pip install -r requirements.txt --system

COPY . .

# Mirrors and worktrees live here. Mount a persistent volume at this path, or
# every cold start re-clones every repository. Declared by the platform (Railway
# volumes, a compose volume, a Cloud Run mount) rather than by a Dockerfile
# VOLUME, which Railway rejects outright.
ENV WORKSPACE_ROOT=/workspace
RUN mkdir -p /workspace

# Railway, Fly and Cloud Run all inject PORT.
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
