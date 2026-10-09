import os
import time
import requests
from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv
from supabase import create_client, Client
from pytubefix import YouTube # yt-dlp ki jagah pytubefix use kar rahe hain

load_dotenv()

app = Flask(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BUCKET_NAME = "songs"
TABLE_NAME = "songs"

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
        # Pytubefix se YouTube video fetch karna
        yt = YouTube(url)
        title = yt.title
        artist = yt.author
        thumb_url = yt.thumbnail_url
        
        # Best audio quality nikalna
        audio_stream = yt.streams.get_audio_only()
        # .m4a format mein download karna
        mp3_path = audio_stream.download(output_path="temp_downloads", filename=f"{timestamp}.m4a")
            
        # Upload Audio to Supabase
        audio_storage_path = f"songs/{timestamp}.m4a"
        with open(mp3_path, "rb") as f:
            supabase.storage.from_(BUCKET_NAME).upload(
                path=audio_storage_path, file=f.read(), file_options={"content-type": "audio/mp4", "upsert": "true"}
            )
        audio_public_url = supabase.storage.from_(BUCKET_NAME).get_public_url(audio_storage_path)

        # Upload Cover Art to Supabase
        cover_public_url = ""
        cover_storage_path = f"covers/{timestamp}.jpg"
        if thumb_url:
            resp = requests.get(thumb_url, timeout=10)
            if resp.status_code == 200:
                supabase.storage.from_(BUCKET_NAME).upload(
                    path=cover_storage_path, file=resp.content, file_options={"content-type": "image/jpeg", "upsert": "true"}
                )
                cover_public_url = supabase.storage.from_(BUCKET_NAME).get_public_url(cover_storage_path)

        # Insert Database Record
        supabase.table(TABLE_NAME).insert({
            "title": title, "artist": artist, "audioUrl": audio_public_url, "coverUrl": cover_public_url
        }).execute()

        return jsonify({"message": "Success", "title": title, "artist": artist})

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        # Clean up temp file
        if mp3_path and os.path.exists(mp3_path):
            os.remove(mp3_path)

@app.route("/api/songs/<int:song_id>", methods=["DELETE"])
def delete_song(song_id):
    try:
        response = supabase.table(TABLE_NAME).select("audioUrl, coverUrl").eq("id", song_id).execute()
        if not response.data:
            return jsonify({"error": "Song not found"}), 404
            
        song = response.data[0]
        supabase.table(TABLE_NAME).delete().eq("id", song_id).execute()
        
        if song.get("audioUrl") and f"/public/{BUCKET_NAME}/" in song["audioUrl"]:
            supabase.storage.from_(BUCKET_NAME).remove([song["audioUrl"].split(f"/public/{BUCKET_NAME}/")[-1]])
        if song.get("coverUrl") and f"/public/{BUCKET_NAME}/" in song["coverUrl"]:
            supabase.storage.from_(BUCKET_NAME).remove([song["coverUrl"].split(f"/public/{BUCKET_NAME}/")[-1]])
            
        return jsonify({"message": "Deleted successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True, port=5000)
