FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    DATA_DIR=/data MUSIC_ROOT=/music SLSKD_DOWNLOADS=/slskd-downloads

# ffmpeg converts YouTube audio to MP3
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# yt-dlp (with its JavaScript helper) and the Deno runtime it needs for YouTube
COPY requirements-youtube.txt .
RUN pip install --no-cache-dir -r requirements-youtube.txt
COPY app ./app

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"
CMD ["uvicorn", "app.web:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
