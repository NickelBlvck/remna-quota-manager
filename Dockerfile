# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC

# tini — сигналы; sqlite3 — ручные операции с БД; tzdata — чтобы работал TZ=
# (daily_summary_hour считается по локальному времени)
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini sqlite3 tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY *.py ./

RUN useradd --system --uid 10001 --home-dir /app app \
    && mkdir -p /app/data \
    && chown -R app:app /app
USER app

VOLUME ["/app/data"]

HEALTHCHECK --interval=2m --timeout=10s --start-period=90s --retries=3 \
    CMD ["python", "healthcheck.py"]

ENTRYPOINT ["tini", "--"]
CMD ["python", "-u", "main.py"]
