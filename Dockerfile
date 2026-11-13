FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.13.7 AS uv
FROM python:3.12-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client rclone ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 workbench \
    && useradd --uid 10001 --gid 10001 --home-dir /var/lib/workbench --create-home workbench
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY container/cli-requirements.txt /tmp/cli-requirements.txt
RUN uv venv /opt/colab-cli \
    && uv pip sync --python /opt/colab-cli/bin/python --require-hashes /tmp/cli-requirements.txt \
    && rm /tmp/cli-requirements.txt
COPY backend/ backend/
COPY templates/ templates/
COPY --from=frontend /build/dist frontend/dist/
COPY container/ container/
RUN chmod -R a+rX backend templates frontend/dist container \
    && chmod 755 container/entrypoint.sh container/colab-account.py
ENV HOME=/var/lib/workbench \
    PATH=/app/.venv/bin:/var/lib/workbench/.local/bin:/usr/local/bin:/usr/bin:/bin \
    WORKBENCH_DATA=/var/lib/workbench/data \
    WORKBENCH_SSH_IDENTITY=/var/lib/workbench/.ssh/id_ed25519 \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/auth/session', timeout=4).close()"
ENTRYPOINT ["/app/container/entrypoint.sh"]
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8765", "--timeout-graceful-shutdown", "420", "--no-access-log"]
