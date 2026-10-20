# TelemetryCraft ships as one process with no third-party dependencies.
FROM python:3.12-slim

WORKDIR /app
COPY craft ./craft
COPY README.md ./

ENV PORT=8787 \
    HOST=0.0.0.0 \
    CRAFT_DB=/data/telemetry.db \
    PYTHONUNBUFFERED=1

RUN mkdir -p /data && useradd -r -u 10001 craft && chown -R craft /data /app
USER craft

EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s \
  CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8787/api/health',timeout=2)"

CMD ["python3", "-m", "craft", "--port", "8787"]
