import os
import time
import requests
from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv
from supabase import create_client, Client
from pytubefix import YouTube

# Load variables from .env (only used locally; Render uses its own Environment Variables)
load_dotenv()

app = Flask(__name__)

# Supabase Credentials
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BUCKET_NAME = "songs"
TABLE_NAME = "songs"

# Initialize Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/songs", methods=["GET"])
def get_songs():
    try:
        response = supabase.table(TABLE_NAME).select("*").order("id").execute()
        return jsonify(response.data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/add", methods=["POST"])
def add_song():
    data = request.json
    url = data.get("url")
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    os.makedirs("temp_downloads", exist_ok=True)
    timestamp = int(time.time() * 1000)
    mp3_path = None

    try:
        # 403 Forbidden Fix: Use ANDROID client to bypass YouTube's bot detection
        yt = YouTube(url, client='ANDROID')
        
        title = yt.title
        artist = yt.author
        thumb_url = yt.thumbnail_url
        
        # Extract highest quality audio stream
        audio_stream = yt.streams.get_audio_only()
        if not audio_stream:
            return jsonify({"error": "Could not find audio for this video"}), 404
            
        # Download audio as .m4a
        mp3_path = audio_stream.download(output_path="temp_downloads", filename=f"{timestamp}.m4a")
            
        # 1. Upload Audio to Supabase
        audio_storage_path = f"songs/{timestamp}.m4a"
        with open(mp3_path, "rb") as f:
            supabase.storage.from_(BUCKET_NAME).upload(
                path=audio_storage_path, 
                file=f.read(), 
                file_options={"content-type": "audio/mp4", "upsert": "true"}
            )
        audio_public_url = supabase.storage.from_(BUCKET_NAME).get_public_url(audio_storage_path)

        # 2. Upload Cover Art to Supabase
        cover_public_url = ""
        cover_storage_path = f"covers/{timestamp}.jpg"
        if thumb_url:
            resp = requests.get(thumb_url, timeout=10)
            if resp.status_code == 200:
                supabase.storage.from_(BUCKET_NAME).upload(
                    path=cover_storage_path, 
                    file=resp.content, 
                    file_options={"content-type": "image/jpeg", "upsert": "true"}
                )
                cover_public_url = supabase.storage.from_(BUCKET_NAME).get_public_url(cover_storage_path)

        # 3. Insert Database Record
        supabase.table(TABLE_NAME).insert({
            "title": title, 
            "artist": artist, 
            "audioUrl": audio_public_url, 
            "coverUrl": cover_public_url
        }).execute()

        return jsonify({"message": "Success", "title": title, "artist": artist})

    except Exception as e:
        return jsonify({"error": str(e)}), 500
        
    finally:
        # Clean up temporary file from Render server to save disk space
        if mp3_path and os.path.exists(mp3_path):
            os.remove(mp3_path)

@app.route("/api/songs/<int:song_id>", methods=["DELETE"])
def delete_song(song_id):
    try:
        # Get URLs to delete files from Storage
        response = supabase.table(TABLE_NAME).select("audioUrl, coverUrl").eq("id", song_id).execute()
        if not response.data:
            return jsonify({"error": "Song not found"}), 404
            
        song = response.data[0]
        
        # Delete row from Database
        supabase.table(TABLE_NAME).delete().eq("id", song_id).execute()
        
        # Delete Audio from Storage
        if song.get("audioUrl") and f"/public/{BUCKET_NAME}/" in song["audioUrl"]:
            audio_path = song["audioUrl"].split(f"/public/{BUCKET_NAME}/")[-1]
            supabase.storage.from_(BUCKET_NAME).remove([audio_path])
            
        # Delete Cover from Storage
        if song.get("coverUrl") and f"/public/{BUCKET_NAME}/" in song["coverUrl"]:
            cover_path = song["coverUrl"].split(f"/public/{BUCKET_NAME}/")[-1]
            supabase.storage.from_(BUCKET_NAME).remove([cover_path])
            
        return jsonify({"message": "Deleted successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    # Render overrides this port, but it runs nicely on 5000 locally
    app.run(debug=True, port=5000)
