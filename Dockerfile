FROM python:3.12-slim-bookworm AS base
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY server/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && useradd -m -u 10001 reel

FROM base AS api
COPY server /app/server
USER reel
CMD ["uvicorn","server.api:app","--host","0.0.0.0","--port","8300","--no-access-log"]

FROM base AS intake
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir yt-dlp==2026.10.26
COPY server /app/server
USER reel
CMD ["uvicorn","server.intake:app","--host","0.0.0.0","--port","8310","--no-access-log"]

FROM base AS worker
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates git libgomp1 && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir uv
COPY --chown=reel:reel . /app
RUN chown reel:reel /app
USER reel
ENV HOME=/home/reel UV_LINK_MODE=copy
RUN mkdir -p /app/.qwen-venv && uv sync --frozen --python /usr/local/bin/python
CMD ["/bin/sh","-ec","model=${REELDUB_MODEL:-small}; .venv/bin/python assets.py \"$model\"; echo \"$model\" > .venv/reeldub-model; exec python -m server.worker"]
