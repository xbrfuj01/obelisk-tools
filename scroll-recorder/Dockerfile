FROM mcr.microsoft.com/playwright/python:v1.47.0-noble

# Chromium + its OS deps are already baked into this base image (matching
# the playwright pip version pinned in requirements.txt) - only ffmpeg needs
# adding, same as the main obelisk image treats it as a plain OS package.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
