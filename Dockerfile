FROM python:3.11-slim

ARG KNG7_IMAGE_TAG=2026-05-02-first-cheap-03
LABEL org.opencontainers.image.title="KNG7 first_cheap_03" \
      org.opencontainers.image.description="Docker: BTC 5m/15m/both (comma BOT_WINDOW_MINUTES) btc50_1c or dual/market" \
      org.opencontainers.image.version="${KNG7_IMAGE_TAG}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN useradd --create-home --shell /usr/sbin/nologin appuser

COPY requirements.txt /app/requirements.txt

RUN pip install --upgrade pip && \
    pip install -r /app/requirements.txt

COPY main.py /app/main.py
COPY cheap03_first_engine.py /app/cheap03_first_engine.py
COPY config.py /app/config.py
COPY trader.py /app/trader.py
COPY market_locator.py /app/market_locator.py
COPY http_session.py /app/http_session.py
COPY clob_fak.py /app/clob_fak.py
COPY polymarket_ws.py /app/polymarket_ws.py

RUN mkdir -p /app/logs /app/exports && \
    chown -R appuser:appuser /app

USER appuser

CMD ["python", "main.py"]
