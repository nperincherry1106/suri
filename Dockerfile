# Microsoft's Playwright Python image already has Python 3.11 + Playwright +
# all browser dependencies pre-installed, which saves ~5 minutes of build time
# vs installing Chromium from scratch on a vanilla python:3.11-slim base.
FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SURI_HEADLESS=1 \
    SURI_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# /data is mounted as a fly volume — DB + outlook_token.json live here so they
# survive deploys.
RUN mkdir -p /data
VOLUME ["/data"]

CMD ["python", "-m", "app.telegram_bot"]
