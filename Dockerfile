FROM postgres:17-alpine AS metadata
RUN apk add --no-cache openssl && install -d -o postgres -g postgres -m 0700 /var/lib/reeldub-tls /var/lib/reeldub-issuer && install -d -o postgres -g postgres -m 0755 /var/lib/reeldub-trust
COPY deploy/postgres/tls.sh deploy/postgres/hba.conf /opt/reeldub/

FROM python:3.12-slim-bookworm AS base
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY LICENSE /usr/share/doc/reeldub/LICENSE
COPY ios/Shared/KittenML /usr/share/doc/reeldub/KittenML
COPY server/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && useradd -m -u 10001 reel

FROM base AS api
COPY server /app/server
USER reel
CMD ["uvicorn","server.api:app","--host","0.0.0.0","--port","8300","--no-access-log"]

FROM python:3.12-slim-bookworm AS proxy
WORKDIR /app
COPY server/__init__.py server/media_network.py server/video_url.py /app/server/
USER 65534:65534
CMD ["python", "-B", "-m", "server.media_network"]

FROM base AS intake
COPY deploy/intake-requirements.txt /tmp/intake-requirements.txt
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir -r /tmp/intake-requirements.txt
COPY server /app/server
USER reel
CMD ["uvicorn","server.intake:app","--host","0.0.0.0","--port","8310","--no-access-log"]

FROM base AS worker
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg sox espeak-ng curl ca-certificates git libgomp1 build-essential && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir uv
COPY --chown=reel:reel pyproject.toml uv.lock ./
RUN mkdir -p /run/reeldub-match /app/.qwen-venv /app/.kitten2 /home/reel/.cache && chown reel:reel /app /run/reeldub-match /app/.qwen-venv /app/.kitten2 /home/reel/.cache && chmod 0700 /run/reeldub-match
USER reel
ENV HOME=/home/reel UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN uv sync --frozen --python /usr/local/bin/python
COPY --chown=reel:reel *.py dub kitten-match setup.sh setup-lab.sh ./
COPY --chown=reel:reel server server
COPY --chown=reel:reel deploy/fleet deploy/fleet
CMD ["python", "-m", "server.worker"]
