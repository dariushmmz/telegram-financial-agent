FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --system --gid 10001 receiptbot \
    && useradd --system --uid 10001 --gid receiptbot --home /app receiptbot

COPY requirements.lock pyproject.toml alembic.ini ./
RUN python -m pip install --upgrade pip && python -m pip install -r requirements.lock

COPY migrations ./migrations
COPY scripts ./scripts
COPY src ./src

RUN mkdir -p /app/data && chown -R receiptbot:receiptbot /app
USER receiptbot

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD ["python", "scripts/manage_runtime.py", "check", "--quiet"]

STOPSIGNAL SIGTERM
CMD ["python", "scripts/run_bot.py"]
