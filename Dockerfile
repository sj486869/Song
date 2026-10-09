FROM python:3.12-slim

# ffmpeg: fallback for formats that need merging/conversion
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl unzip \
    && rm -rf /var/lib/apt/lists/*

# Install Deno (JavaScript runtime yt-dlp uses for YouTube)
RUN curl -fsSL https://deno.land/install.sh | sh \
    && mv /root/.deno/bin/deno /usr/local/bin/deno

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -U -r requirements.txt

# Install yt-dlp from GitHub master (PyPI is often days behind)
RUN pip install --no-cache-dir --force-reinstall \
    https://github.com/yt-dlp/yt-dlp/archive/refs/heads/master.zip

COPY . .

# Render sets $PORT (default 10000)
CMD gunicorn app:app --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 4 --timeout 300
