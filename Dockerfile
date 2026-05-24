FROM python:3.11-slim

ARG KNG7_IMAGE_TAG=2026-05-24-limit-pair-5m
LABEL org.opencontainers.image.title="KNG7 limit_pair_5m" \
      org.opencontainers.image.description="Docker: scheduled BTC 5m UP/DOWN GTC limits (50c/49c)" \
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
COPY limit_pair_engine.py /app/limit_pair_engine.py
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
