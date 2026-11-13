FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[discord]" && rm -rf /build \
 && useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin peerpass \
 && mkdir -p /data && chown peerpass /data

USER 10001
WORKDIR /data
ENV PEERPASS_STATE_DIR=/data PEERPASS_LISTEN=0.0.0.0:8750
EXPOSE 8750
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8750/health', timeout=3)"]
ENTRYPOINT ["peerpass"]
CMD ["serve"]
