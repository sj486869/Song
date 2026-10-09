FROM python:3.9-slim

# FFmpeg install karna zaroori hai yt-dlp ke liye
RUN apt-get update && apt-get install -y ffmpeg

# Working directory set karein
WORKDIR /app

# Saare files ko container me copy karein
COPY . .

# Python libraries install karein
RUN pip install --no-cache-dir -r requirements.txt

# Port expose karein
EXPOSE 5000

# Gunicorn ke through Flask app ko run karein
CMD ["gunicorn", "-b", "0.0.0.0:5000", "app:app"]