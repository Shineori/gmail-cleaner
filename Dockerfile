FROM ghcr.io/astral-sh/uv:0.9.7 AS uv

FROM python:3.11-slim

WORKDIR /app

# Unbuffered Python output (for Docker logs), web auth mode for Docker
ENV PYTHONUNBUFFERED=1 \
    WEB_AUTH=true \
    UV_COMPILE_BYTECODE=1

# uv pinned to a fixed version (not :latest)
COPY --from=uv /uv /usr/local/bin/uv

# Dependencies only, exact versions from the lockfile, no test tools
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev

# Application
COPY . .

# Never ship a token; run as an unprivileged user that owns only /app/data
RUN rm -f token.json \
 && useradd --uid 1000 --create-home appuser \
 && mkdir -p /app/data \
 && chown -R appuser:appuser /app/data
USER appuser

# 8766 web UI, 8767 OAuth callback (only needed when the browser is on the same machine)
EXPOSE 8766 8767

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8766/healthz', timeout=4)"]

CMD ["/app/.venv/bin/python", "main.py"]
