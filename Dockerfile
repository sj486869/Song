# 1. Update to Python 3.11 to fix the deprecation warning
FROM python:3.11-slim

# 2. Install FFmpeg
RUN apt-get update && apt-get install -y ffmpeg

# 3. Set Working Directory
WORKDIR /app

# 4. Copy Files
COPY . .

# 5. Install Libraries
RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 5000

# 6. FIX: Add --timeout 120 to give yt-dlp enough time to download and convert the song
CMD ["gunicorn", "--timeout", "120", "-b", "0.0.0.0:5000", "app:app"]
