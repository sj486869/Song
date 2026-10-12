import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

import requests
import yt_dlp
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from supabase import Client, create_client

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

app = Flask(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BUCKET_NAME = "songs"
TABLE_NAME = "songs"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID")

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

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set in .env")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

print(f"yt-dlp version : {yt_dlp.version.__version__}")
print(f"Cookies file : {COOKIES_SRC} -> exists={os.path.exists(COOKIES_SRC)}")
print(f"Proxy : {PROXY or 'not set'}")
print(f"Telegram : {'configured' if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else 'not configured'}")


def send_telegram_message(message, chat_id=None):
    target_chat_id = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not target_chat_id:
        return False

    payload = {
        "chat_id": target_chat_id,
        "text": message,
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=10,
        )
        response.raise_for_status()
        return bool(response.json().get("ok"))
    except requests.RequestException:
        return False


def download_with_subprocess(url, timestamp):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    outtmpl = os.path.join(DOWNLOAD_DIR, f"{timestamp}.%(ext)s")
    cookie_copy = None

    base_cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--format",
        "bestaudio[ext=m4a]/bestaudio/best",
        "-o",
        outtmpl,
        "--no-playlist",
        "--socket-timeout",
        "30",
        "--retries",
        "3",
    ]
    if PROXY:
        base_cmd += ["--proxy", PROXY]
    if os.path.exists(COOKIES_SRC):
        cookie_copy = os.path.join(DOWNLOAD_DIR, f"{timestamp}_cookies.txt")
        shutil.copy(COOKIES_SRC, cookie_copy)
        base_cmd += ["--cookiefile", cookie_copy]

    cmd = base_cmd + ["--dump-json", "--skip-download", url]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    if result.returncode != 0:
        for client in ["ios", "tv", "mweb"]:
            retry_cmd = base_cmd + [
                "--extractor-args",
                f"youtube:player_client={client}",
                "--dump-json",
                "--skip-download",
                url,
            ]
            result = subprocess.run(
                retry_cmd, capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0:
                break

        if result.returncode != 0:
            raise Exception(f"Metadata failed: {result.stderr[-500:]}")

    info = json.loads(result.stdout)
    duration = info.get("duration")
    if duration and duration > MAX_DURATION:
        raise Exception(f"Video too long (max {MAX_DURATION // 60} minutes)")

    result = subprocess.run(base_cmd + [url], capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise Exception(f"Download failed: {result.stderr[-500:]}")

    matches = [
        path
        for path in glob.glob(os.path.join(DOWNLOAD_DIR, f"{timestamp}.*"))
        if not path.endswith(".part") and not path.endswith(".txt")
    ]
    if not matches:
        raise Exception("Downloaded file not found")

    info["filepath"] = matches[0]
    return info


def add_song_from_url(url):
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
            raise RuntimeError("Could not find audio file")

        extension = os.path.splitext(file_path)[1].lstrip(".").lower() or "m4a"
        content_type = CONTENT_TYPES.get(extension, "application/octet-stream")
        audio_storage_path = f"songs/{timestamp}.{extension}"
        with open(file_path, "rb") as audio_file:
            supabase.storage.from_(BUCKET_NAME).upload(
                path=audio_storage_path,
                file=audio_file.read(),
                file_options={"content-type": content_type, "upsert": "true"},
            )
        audio_public_url = supabase.storage.from_(BUCKET_NAME).get_public_url(
            audio_storage_path
        )

        cover_public_url = ""
        if thumb_url:
            try:
                response = requests.get(thumb_url, timeout=10)
                if response.status_code == 200:
                    cover_storage_path = f"covers/{timestamp}.jpg"
                    supabase.storage.from_(BUCKET_NAME).upload(
                        path=cover_storage_path,
                        file=response.content,
                        file_options={
                            "content-type": "image/jpeg",
                            "upsert": "true",
                        },
                    )
                    cover_public_url = supabase.storage.from_(BUCKET_NAME).get_public_url(
                        cover_storage_path
                    )
            except requests.RequestException:
                pass

        supabase.table(TABLE_NAME).insert(
            {
                "title": title,
                "artist": artist,
                "audioUrl": audio_public_url,
                "coverUrl": cover_public_url,
            }
        ).execute()
        return {"title": title, "artist": artist}
    finally:
        for path in glob.glob(os.path.join(DOWNLOAD_DIR, f"{timestamp}*")):
            try:
                os.remove(path)
            except OSError:
                pass


def extract_youtube_url(text):
    for candidate in re.findall(r"https?://[^\s<>]+", text or ""):
        candidate = candidate.rstrip(".,!?;:)]}")
        hostname = (urlsplit(candidate).hostname or "").lower()
        if (
            hostname == "youtu.be"
            or hostname.endswith(".youtu.be")
            or hostname == "youtube.com"
            or hostname.endswith(".youtube.com")
        ):
            return candidate
    return None


def telegram_poll_loop():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    endpoint = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    offset = None
    while True:
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            response = requests.get(endpoint, params=params, timeout=35)
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                raise RuntimeError("Telegram getUpdates returned an error")

            for update in payload.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message") or update.get("channel_post")
                if not message:
                    continue

                chat_id = str(message.get("chat", {}).get("id", ""))
                if chat_id != str(TELEGRAM_CHAT_ID):
                    continue

                text = message.get("text") or message.get("caption") or ""
                url = extract_youtube_url(text)
                if not url:
                    send_telegram_message(
                        "Send a YouTube video link to add a song.", chat_id=chat_id
                    )
                    continue

                send_telegram_message("Adding your song...", chat_id=chat_id)
                try:
                    song = add_song_from_url(url)
                    send_telegram_message(
                        f"Added: {song['title']} - {song['artist']}",
                        chat_id=chat_id,
                    )
                except Exception as error:
                    send_telegram_message(
                        f"Could not add that song: {str(error)[:300]}",
                        chat_id=chat_id,
                    )
        except (requests.RequestException, ValueError, RuntimeError) as error:
            print(f"Telegram polling error: {error}")
            time.sleep(5)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/debug")
def debug_info():
    cookies_exist = os.path.exists(COOKIES_SRC)
    cookie_count = 0
    if cookies_exist:
        with open(COOKIES_SRC, encoding="utf-8") as cookie_file:
            cookie_count = len(
                [
                    line
                    for line in cookie_file.read().splitlines()
                    if line and not line.startswith("#")
                ]
            )

    proxy_test = "not tested"
    if PROXY:
        try:
            result = subprocess.run(
                [
                    "curl",
                    "-s",
                    "-o",
                    "/dev/null",
                    "-w",
                    "%{http_code}",
                    "--proxy",
                    PROXY,
                    "--max-time",
                    "10",
                    "https://www.youtube.com/robots.txt",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            proxy_test = f"HTTP {result.stdout.strip()}"
        except Exception as error:
            proxy_test = f"failed: {error}"

    return jsonify(
        {
            "yt_dlp_version": yt_dlp.version.__version__,
            "cookies_exist": cookies_exist,
            "cookie_count": cookie_count,
            "proxy": PROXY or "not set",
            "proxy_test": proxy_test,
            "telegram_configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        }
    )


@app.route("/api/telegram/test")
def test_telegram():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return jsonify({"status": "not_configured"}), 400

    message = "Song app Telegram notification is working."
    sent = send_telegram_message(message)
    return jsonify({"status": "sent" if sent else "failed"})


@app.route("/api/test")
def test_download():
    url = request.args.get(
        "url", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    )
    timestamp = int(time.time() * 1000)
    try:
        info = download_with_subprocess(url, timestamp)
        result_data = {
            "status": "SUCCESS",
            "title": info.get("title"),
            "duration": info.get("duration"),
        }
    except Exception as error:
        result_data = {"status": "FAILED", "error": str(error)}
    finally:
        for path in glob.glob(os.path.join(DOWNLOAD_DIR, f"{timestamp}*")):
            try:
                os.remove(path)
            except OSError:
                pass
    return jsonify(result_data)


@app.route("/api/songs", methods=["GET"])
def get_songs():
    try:
        response = supabase.table(TABLE_NAME).select("*").order("id").execute()
        return jsonify(response.data)
    except Exception as error:
        return jsonify({"error": str(error)}), 500


@app.route("/api/add", methods=["POST"])
def add_song():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        song = add_song_from_url(url)
        send_telegram_message(f"🎵 Added song: {song['title']} — {song['artist']}")
        return jsonify({"message": "Success", **song})
    except Exception as error:
        return jsonify({"error": str(error)}), 500


@app.route("/api/songs/<int:song_id>", methods=["DELETE"])
def delete_song(song_id):
    try:
        response = (
            supabase.table(TABLE_NAME)
            .select("title, artist, audioUrl, coverUrl")
            .eq("id", song_id)
            .execute()
        )
        if not response.data:
            return jsonify({"error": "Song not found"}), 404

        song = response.data[0]
        title = song.get("title") or "Unknown title"
        artist = song.get("artist") or "Unknown artist"
        supabase.table(TABLE_NAME).delete().eq("id", song_id).execute()

        if song.get("audioUrl") and f"/public/{BUCKET_NAME}/" in song["audioUrl"]:
            audio_path = song["audioUrl"].split(f"/public/{BUCKET_NAME}/")[-1]
            supabase.storage.from_(BUCKET_NAME).remove([audio_path])
        if song.get("coverUrl") and f"/public/{BUCKET_NAME}/" in song["coverUrl"]:
            cover_path = song["coverUrl"].split(f"/public/{BUCKET_NAME}/")[-1]
            supabase.storage.from_(BUCKET_NAME).remove([cover_path])

        send_telegram_message(f"🗑️ Deleted song: {title} — {artist}")
        return jsonify({"message": "Deleted successfully"})
    except Exception as error:
        return jsonify({"error": str(error)}), 500


if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG") == "1"
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and (
        not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true"
    ):
        threading.Thread(target=telegram_poll_loop, daemon=True).start()
        print("Telegram link listener started")

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5000)),
        debug=debug,
    )
