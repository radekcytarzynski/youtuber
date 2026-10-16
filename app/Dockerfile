FROM python:3.12-slim

# yt-dlp potrzebuje środowiska JavaScript (Deno) do rozwiązywania zabezpieczeń YouTube.
COPY --from=denoland/deno:bin-2.1.4 /deno /usr/local/bin/deno

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    XDG_CACHE_HOME=/tmp/cache \
    DENO_DIR=/tmp/deno \
    DATA_DIR=/data

RUN useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown app:app /data

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .

USER app
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--no-server-header"]
