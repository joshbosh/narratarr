FROM python:3.12-slim

ARG VERSION=dev
ARG REVISION=
LABEL org.opencontainers.image.title="Narratarr" \
      org.opencontainers.image.description="Request audiobooks by Audible release and fetch them through slskd, tagged and ready for Audiobookshelf" \
      org.opencontainers.image.source="https://github.com/joshbosh/narratarr" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"

# ffmpeg: chapter embedding for single-file m4b books (optional but recommended)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY web ./web

ENV NARRATARR_VERSION=${VERSION} \
    NARRATARR_REVISION=${REVISION} \
    NARRATARR_DATA_DIR=/config \
    PYTHONUNBUFFERED=1
VOLUME ["/config", "/downloads"]
EXPOSE 8787

HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8787/api/system/status')" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787", "--proxy-headers"]
