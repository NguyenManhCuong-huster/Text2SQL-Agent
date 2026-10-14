# syntax=docker/dockerfile:1

# ---- build stage: install dependencies into an isolated venv ----
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install -r requirements.txt

# ---- runtime stage ----
FROM python:3.12-slim
# FORWARDED_ALLOW_IPS: only trust X-Forwarded-For from these proxies (set to your reverse proxy's IP).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    DATA_DIR=/data \
    PORT=8000 \
    WORKERS=2 \
    FORWARDED_ALLOW_IPS=127.0.0.1

RUN useradd --create-home --uid 1000 app \
 && mkdir -p /data && chown app:app /data
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=app:app app ./app

USER app
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/api/health', timeout=4)" || exit 1

# Build the sample DB once before workers start so they don't race on it.
CMD ["sh", "-c", "python -c 'from app import sample_data, history; history.init(); sample_data.ensure_sample()' && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers ${WORKERS} --proxy-headers"]
