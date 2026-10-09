FROM python:3.12-slim

# ffmpeg: fallback for formats that need merging/conversion
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Deno: JavaScript runtime that recent yt-dlp versions use for YouTube
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app

COPY requirements.txt .
# -U pulls the newest yt-dlp every time the image is rebuilt
RUN pip install --no-cache-dir -U -r requirements.txt

COPY . .

# Render sets $PORT (default 10000). Long timeout because downloads happen inside the request.
CMD gunicorn app:app --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 4 --timeout 300
