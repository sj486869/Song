# 1. Update to Python 3.11
FROM python:3.11-slim

# 2. Install FFmpeg AND Git (git is needed to download yt-dlp from github)
RUN apt-get update && apt-get install -y ffmpeg git

# 3. Set Working Directory
WORKDIR /app

# 4. Copy Files
COPY . .

# 5. Install Libraries
RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 5000

# 6. Run Gunicorn with a 120-second timeout
CMD ["gunicorn", "--timeout", "120", "-b", "0.0.0.0:5000", "app:app"]
