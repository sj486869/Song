import glob
import os
import shutil
import time

import requests
import yt_dlp
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from supabase import Client, create_client

load_dotenv()

app = Flask(__name__)

# Supabase credentials
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BUCKET_NAME = "songs"
TABLE_NAME = "songs"

# Optional YouTube settings
COOKIES_SRC = os.getenv("YT_COOKIES_FILE", "/etc/secrets/cookies.txt")
PROXY = os.getenv("YT_PROXY")
MAX_DURATION = int(os.getenv("MAX_DURATION_SECONDS", "1200"))
DOWNLOAD_DIR = "temp_downloads"

CONTENT_TYPES = {
    "m4a": "audio/mp4",
    "mp4": "audio/mp4",
    "webm": "audio/webm",
    "opus": "audio/ogg",
    "ogg": "audio/ogg",
    "mp3": "audio/mpeg",
}

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Startup info ──────────────────────────────────────────────
print(f"yt-dlp version: {yt_dlp.version.__version__}")
print(f"Cookies file:   {COOKIES_SRC} → exists={os.path.exists(COOKIES_SRC)}")
if os.path.exists(COOKIES_SRC):
    with open(COOKIES_SRC) as f:
        lines = [l for l in f.read().splitlines() if l and not l.startswith("#")]
        print(f"Cookies loaded:  {len(lines)} entries")
# ──────────────────────────────────────────────────────────────


def build_ydl_opts(timestamp):
    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": os.path.join(DOWNLOAD_DIR, f"{timestamp}.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        # Helps with bot detection on some versions
        "extractor_args": {"youtube": {"player_client": ["web"]}},
    }
    if PROXY:
        opts["proxy"] = PROXY
    if os.path.exists(COOKIES_SRC):
        cookie_copy = os.path.join(DOWNLOAD_DIR, f"{timestamp}_cookies.txt")
        shutil.copy(COOKIES_SRC, cookie_copy)
        opts["cookiefile"] = cookie_copy
        print(f"Using cookies from: {cookie_copy}")
    else:
        print("WARNING: No cookies file found — YouTube will likely block you")
    return opts


@app.route("/")
def index():
    return render_template("index.html")


# ── Debug endpoint ────────────────────────────────────────────
@app.route("/api/debug")
def debug_info():
    cookies_exist = os.path.exists(COOKIES_SRC)
    cookie_count = 0
    if cookies_exist:
        with open(COOKIES_SRC) as f:
            cookie_count = len(
                [l for l in f.read().splitlines() if l and not l.startswith("#")]
            )
    return jsonify({
        "yt_dlp_version": yt_dlp.version.__version__,
        "cookies_file": COOKIES_SRC,
        "cookies_exist": cookies_exist,
        "cookie_count": cookie_count,
        "proxy": PROXY or "not set",
    })
# ──────────────────────────────────────────────────────────────


@app.route("/api/songs", methods=["GET"])
def get_songs():
    try:
        response = supabase.table(TABLE_NAME).select("*").order("id").execute()
        return jsonify(response.data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/add", methods=["POST"])
def add_song():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    timestamp = int(time.time() * 1000)

    try:
        opts = build_ydl_opts(timestamp)

        # First attempt
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                duration = info.get("duration")
                if duration and duration > MAX_DURATION:
                    return jsonify(
                        {"error": f"Video too long (max {MAX_DURATION // 60} minutes)"}
                    ), 400
                info = ydl.process_ie_result(info, download=True)
        except yt_dlp.utils.DownloadError as first_err:
            print(f"First attempt failed: {first_err}")

            # Retry with verbose logging so we can see what's wrong
            opts["verbose"] = True
            opts["print"] = "debug"
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    duration = info.get("duration")
                    if duration and duration > MAX_DURATION:
                        return jsonify(
                            {"error": f"Video too long (max {MAX_DURATION // 60} minutes)"}
                        ), 400
                    info = ydl.process_ie_result(info, download=True)
            except Exception as retry_err:
                return jsonify({
                    "error": f"Download failed after retry: {retry_err}",
                    "first_error": str(first_err),
                    "yt_dlp_version": yt_dlp.version.__version__,
                    "cookies_found": os.path.exists(COOKIES_SRC),
                }), 502

        title = info.get("track") or info.get("title") or "Unknown title"
        artist = (
            info.get("artist")
            or info.get("channel")
            or info.get("uploader")
            or "Unknown artist"
        )
        thumb_url = info.get("thumbnail")

        # Locate the downloaded file
        downloads = info.get("requested_downloads") or []
        file_path = downloads[0].get("filepath") if downloads else None
        if not file_path or not os.path.exists(file_path):
            matches = [
                p
                for p in glob.glob(os.path.join(DOWNLOAD_DIR, f"{timestamp}.*"))
                if not p.endswith(".part")
            ]
            file_path = matches[0] if matches else None
        if not file_path:
            return jsonify({"error": "Could not find audio for this video"}), 404

        ext = os.path.splitext(file_path)[1].lstrip(".").lower() or "m4a"
        content_type = CONTENT_TYPES.get(ext, "application/octet-stream")

        # 1. Upload audio to Supabase
        audio_storage_path = f"songs/{timestamp}.{ext}"
        with open(file_path, "rb") as f:
            supabase.storage.from_(BUCKET_NAME).upload(
                path=audio_storage_path,
                file=f.read(),
                file_options={"content-type": content_type, "upsert": "true"},
            )
        audio_public_url = supabase.storage.from_(BUCKET_NAME).get_public_url(
            audio_storage_path
        )

        # 2. Upload cover art to Supabase
        cover_public_url = ""
        if thumb_url:
            try:
                resp = requests.get(thumb_url, timeout=10)
                if resp.status_code == 200:
                    cover_storage_path = f"covers/{timestamp}.jpg"
                    supabase.storage.from_(BUCKET_NAME).upload(
                        path=cover_storage_path,
                        file=resp.content,
                        file_options={"content-type": "image/jpeg", "upsert": "true"},
                    )
                    cover_public_url = supabase.storage.from_(BUCKET_NAME).get_public_url(
                        cover_storage_path
                    )
            except requests.RequestException:
                pass

        # 3. Insert database record
        supabase.table(TABLE_NAME).insert(
            {
                "title": title,
                "artist": artist,
                "audioUrl": audio_public_url,
                "coverUrl": cover_public_url,
            }
        ).execute()

        return jsonify({"message": "Success", "title": title, "artist": artist})

    except yt_dlp.utils.DownloadError as e:
        return jsonify({
            "error": f"Download failed: {e}",
            "yt_dlp_version": yt_dlp.version.__version__,
            "cookies_found": os.path.exists(COOKIES_SRC),
        }), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        for p in glob.glob(os.path.join(DOWNLOAD_DIR, f"{timestamp}*")):
            try:
                os.remove(p)
            except OSError:
                pass


@app.route("/api/songs/<int:song_id>", methods=["DELETE"])
def delete_song(song_id):
    try:
        response = (
            supabase.table(TABLE_NAME)
            .select("audioUrl, coverUrl")
            .eq("id", song_id)
            .execute()
        )
        if not response.data:
            return jsonify({"error": "Song not found"}), 404

        song = response.data[0]
        supabase.table(TABLE_NAME).delete().eq("id", song_id).execute()

        if song.get("audioUrl") and f"/public/{BUCKET_NAME}/" in song["audioUrl"]:
            audio_path = song["audioUrl"].split(f"/public/{BUCKET_NAME}/")[-1]
            supabase.storage.from_(BUCKET_NAME).remove([audio_path])

        if song.get("coverUrl") and f"/public/{BUCKET_NAME}/" in song["coverUrl"]:
            cover_path = song["coverUrl"].split(f"/public/{BUCKET_NAME}/")[-1]
            supabase.storage.from_(BUCKET_NAME).remove([cover_path])

        return jsonify({"message": "Deleted successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5000)),
        debug=os.getenv("FLASK_DEBUG") == "1",
    )
