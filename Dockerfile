# syntax=docker/dockerfile:1

# ---- build: install pinned dependencies into a virtualenv -----------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install -r requirements.txt

# ---- runtime: slim image, non-root user ------------------------------------------
FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    WEB_CONCURRENCY=1 \
    FORWARDED_ALLOW_IPS=127.0.0.1

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv
WORKDIR /srv
COPY app ./app

USER app
EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/health/live', timeout=3)"]

# WEB_CONCURRENCY sets the worker count (read by uvicorn). The rate limiter and
# auth cache are per worker until Redis is added; see README "Performance".
CMD ["uvicorn", "--factory", "app.main:create_app", \
     "--host", "0.0.0.0", "--port", "9000", \
     "--loop", "uvloop", "--http", "httptools", \
     "--no-access-log", "--proxy-headers", "--timeout-graceful-shutdown", "30"]
