FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/runtime/jobs

EXPOSE 8080

CMD ["sh", "-c", "gunicorn app:app --workers 1 --threads 8 --timeout 0 --graceful-timeout 30 --no-control-socket --bind 0.0.0.0:${PORT:-8080}"]
