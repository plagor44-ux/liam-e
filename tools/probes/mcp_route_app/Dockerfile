# syntax=docker/dockerfile:1
# Image of the synthetic MCP-route probe app (#157). Test-only: it holds no data and needs no
# secret. The build context is this directory; deploy.sh uploads it from a clean copy.
FROM python:3.14-slim AS probe
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv
COPY requirements.txt ./
# Exactly the hashed wheels in requirements.txt, nothing resolved at build time.
RUN pip install --require-hashes --no-deps --only-binary=:all: -r requirements.txt
COPY probe_app ./probe_app
RUN groupadd -g 1000 app && \
    useradd -u 1000 -g 1000 -M -s /usr/sbin/nologin app
USER 1000:1000
ENV HOST=0.0.0.0 PORT=3000
EXPOSE 3000
HEALTHCHECK --interval=10s --timeout=5s --retries=5 --start-period=30s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:${PORT:-3000}/api/health', timeout=5).status==200 else 1)"
CMD ["sh", "-c", "exec uvicorn --app-dir /srv probe_app.main:app --host \"$HOST\" --port \"$PORT\" --proxy-headers --no-access-log"]
