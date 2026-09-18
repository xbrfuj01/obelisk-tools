FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends make cpanminus \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ExifTool ships new format/metadata support (C2PA support is recent)
# faster than Debian's packaged libimage-exiftool-perl catches up - always
# re-pulled to latest on every image build regardless of Docker layer
# cache. CACHEBUST is passed as a unique value per build.
ARG CACHEBUST=1
RUN echo "cachebust=${CACHEBUST}" \
    && cpanm --notest Image::ExifTool

COPY app ./app

RUN mkdir -p /downloads

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
