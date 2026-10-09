FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl unzip \
    && rm -rf /var/lib/apt/lists/*

# Install Deno (yt-dlp needs it for YouTube)
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -U -r requirements.txt

# Install yt-dlp from GitHub (PyPI lags behind YouTube changes)
RUN pip install --no-cache-dir --force-reinstall \
    https://github.com/yt-dlp/yt-dlp/archive/refs/heads/master.zip

COPY . .

CMD gunicorn app:app --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 4 --timeout 300
