FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl unzip \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JS runtime to solve YouTube's player challenge - without
# one it silently falls back to a more restricted client that reports some
# videos as unplayable/missing formats even though a real browser handles
# them fine. Deno is yt-dlp's own default runtime to look for, and
# DENO_INSTALL puts the binary straight on PATH without a manual mv.
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# yt-dlp's site extractors (YouTube, TikTok, VK, ...) break and get fixed
# constantly - re-pulled to latest on every image build regardless of
# Docker layer cache. CACHEBUST is passed as a unique value per build.
#
# yt-dlp-ejs is the challenge-solving script Deno above actually runs.
# bgutil-ytdlp-pot-provider talks to the bgutil-provider sidecar (see
# docker-compose.yml) to get YouTube's "proof of origin" tokens - without
# one, YouTube now forces SABR (no usable format URL) on every client this
# module uses, so this isn't optional the way it might once have been.
ARG CACHEBUST=1
RUN echo "cachebust=${CACHEBUST}" \
    && pip install --no-cache-dir --upgrade yt-dlp yt-dlp-ejs bgutil-ytdlp-pot-provider

COPY app ./app

RUN mkdir -p /data /downloads

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
