import glob
import os
import shutil
import time
import subprocess
import json

import requests
import yt_dlp
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from supabase import Client, create_client

load_dotenv()

app = Flask(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BUCKET_NAME = "songs"
TABLE_NAME = "songs"

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
print(f"yt-dlp version : {yt_dlp.version.__version__}")
print(f"Cookies file   : {COOKIES_SRC} → exists={os.path.exists(COOKIES_SRC)}")
print(f"Proxy          : {PROXY or 'not set'}")
try:
    r = subprocess.run(["deno", "--version"], capture_output=True, text=True, timeout=5)
    print(f"Deno           : {r.stdout.strip()}")
except Exception:
    print(f"Deno           : not found")
# ──────────────────────────────────────────────────────────────


def download_with_subprocess(url, timestamp):
    """Run yt-dlp as a subprocess. curl handles proxy auth correctly."""
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    outtmpl = os.path.join(DOWNLOAD_DIR, f"{timestamp}.%(ext)s")

    cmd = [
        "yt-dlp",
        "--format", "bestaudio[ext=m4a]/bestaudio/best",
        "--outtmpl", outtmpl,
        "--noplaylist",
        "--socket-timeout", "30",
        "--retries", "3",
        "--dump-json",
        "--no-download",  # first pass: metadata only
    ]

    if PROXY:
        cmd += ["--proxy", PROXY]
    if os.path.exists(COOKIES_SRC):
        cookie_copy = os.path.join(DOWNLOAD_DIR, f"{timestamp}_cookies.txt")
        shutil.copy(COOKIES_SRC, cookie_copy)
        cmd += ["--cookiefile", cookie_copy]

    cmd.append(url)

    print(f"Running: {' '.join(cmd[:8])}...")

    # Pass 1: get metadata
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        # Try with different player clients
        for client in ["ios", "tv", "mweb"]:
            cmd_retry = cmd.copy()
            cmd_retry.insert(1, "--extractor-args")
            cmd_retry.insert(2, f"youtube:player_client={client}")
            print(f"Retrying with player_client={client}")
            result = subprocess.run(cmd_retry, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                break

        if result.returncode != 0:
            raise Exception(f"yt-dlp metadata failed: {result.stderr[-500:]}")

    info = json.loads(result.stdout)
    duration = info.get("duration")
    if duration and duration > MAX_DURATION:
        raise Exception(f"Video too long (max {MAX_DURATION // 60} minutes)")

    # Pass 2: download the audio
    cmd_download = [
        "yt-dlp",
        "--format", "bestaudio[ext=m4a]/bestaudio/best",
        "--outtmpl", outtmpl,
        "--noplaylist",
        "--socket-timeout", "30",
        "--retries", "3",
    ]
    if PROXY:
        cmd_download += ["--proxy", PROXY]
    if os.path.exists(COOKIES_SRC):
        cmd_download += ["--cookiefile", cookie_copy]

    cmd_download.append(url)

    result = subprocess.run(cmd_download, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise Exception(f"yt-dlp download failed: {result.stderr[-500:]}")

    # Find the file
    matches = [
        p for p in glob.glob(os.path.join(DOWNLOAD_DIR, f"{timestamp}.*"))
        if not p.endswith(".part") and not p.endswith(".txt")
    ]
    if not matches:
        raise Exception("Downloaded file not found")

    info["filepath"] = matches[0]
    return info


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/debug")
def debug_info():
    cookies_exist = os.path.exists(COOKIES_SRC)
    cookie_count = 0
    if cookies_exist:
        with open(COOKIES_SRC) as f:
            cookie_count = len(
                [l for l in f.read().splitlines() if l and not l.startswith("#")]
            )
    deno_ver = "not found"
    try:
        r = subprocess.run(["deno", "--version"], capture_output=True, text=True, timeout=5)
        deno_ver = r.stdout.strip()
    except Exception:
        pass
    # Test proxy with curl
    proxy_test = "not tested"
    if PROXY:
        try:
            r = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 "--proxy", PROXY, "--max-time", "10",
                 "https://www.youtube.com/robots.txt"],
                capture_output=True, text=True, timeout=15
            )
            proxy_test = f"HTTP {r.stdout.strip()} (curl)"
        except Exception as e:
            proxy_test = f"failed: {e}"
    return jsonify({
        "yt_dlp_version": yt_dlp.version.__version__,
        "cookies_exist": cookies_exist,
        "cookie_count": cookie_count,
        "proxy": PROXY or "not set",
        "proxy_test": proxy_test,
        "deno": deno_ver,
    })


@app.route("/api/test")
def test_download():
    url = request.args.get("url", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    timestamp = int(time.time() * 1000)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    try:
        info = download_with_subprocess(url, timestamp)
        result = {
            "status": "SUCCESS",
            "title": info.get("title"),
            "duration": info.get("duration"),
        }
    except Exception as e:
        result = {
            "status": "FAILED",
            "error": str(e),
        }
    finally:
        for p in glob.glob(os.path.join(DOWNLOAD_DIR, f"{timestamp}*")):
            try:
                os.remove(p)
            except OSError:
                pass

    return jsonify(result)


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

    timestamp = int(time.time() * 1000)

    try:
        info = download_with_subprocess(url, timestamp)

        title = info.get("track") or info.get("title") or "Unknown title"
        artist = (
            info.get("artist")
            or info.get("channel")
            or info.get("uploader")
            or "Unknown artist"
        )
        thumb_url = info.get("thumbnail")

        file_path = info.get("filepath")
        if not file_path or not os.path.exists(file_path):
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
