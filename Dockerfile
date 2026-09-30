FROM python:3.13-alpine

LABEL org.opencontainers.image.title="pixiv-danbooru-bridge" \
      org.opencontainers.image.description="Danbooru-compatible API in front of pixiv, with Danbooru tag mapping"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BRIDGE_HOST=0.0.0.0 \
    BRIDGE_PORT=8000 \
    DB_PATH=/data/mapping.sqlite3

RUN apk add --no-cache tzdata \
 && adduser -D -H -u 10001 bridge \
 && mkdir /data && chown bridge /data
WORKDIR /app
COPY bridge ./bridge
USER bridge
VOLUME /data

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('BRIDGE_PORT','8000'), timeout=8)" || exit 1

CMD ["python", "-m", "bridge"]
