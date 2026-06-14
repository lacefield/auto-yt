import os
import sys
import time
import asyncio
import random
import logging
import subprocess
import textwrap
from pathlib import Path

import requests
import wikipedia
import edge_tts
from PIL import Image, ImageDraw, ImageFont

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ---------- Config ----------
WORKDIR = Path.cwd()
VOICE_FILE = WORKDIR / "voice.mp3"
VIDEO_FILE = WORKDIR / "final_video.mp4"
THUMB_FILE = WORKDIR / "thumb.jpg"
NUM_IMAGES = 5
SECONDS_PER_IMAGE = 3
FRAMERATE = 30
VIDEO_RES = (1280, 720)
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
LOGFILE = WORKDIR / "robot.log"
# ----------------------------

# ---------- Logging ----------
logging.basicConfig(
    filename=str(LOGFILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("auto-yt")
# ----------------------------

# ---------- Secrets from env (secure) ----------
AMAZON_ID = os.environ.get("AMAZON_ID")
CLIENT_ID = os.environ.get("CLIENT_ID")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET")
REFRESH_TOKEN = os.environ.get("REFRESH_TOKEN")

def require_env(name, value):
    if not value:
        logger.error("Missing required environment variable: %s", name)
        raise SystemExit(f"Missing required environment variable: {name}")

require_env("AMAZON_ID", AMAZON_ID)
require_env("CLIENT_ID", CLIENT_ID)
require_env("CLIENT_SECRET", CLIENT_SECRET)
require_env("REFRESH_TOKEN", REFRESH_TOKEN)
# ----------------------------

# ---------- Helpers ----------
def safe_wikipedia_page(max_attempts=12):
    for _ in range(max_attempts):
        try:
            title = wikipedia.random(1)
            page = wikipedia.page(title)
            if len(page.summary) > 50:
                return page
        except Exception as e:
            logger.debug("wikipedia attempt failed: %s", e)
            time.sleep(0.2)
    raise RuntimeError("Could not fetch a suitable Wikipedia page.")

def build_script(title, summary, max_chars=600):
    hooks = [
        f"Stop scrolling. {title} is crazier than you think.",
        f"You've heard of {title}, but not like this.",
        f"Most people have no idea how wild {title} really is."
    ]
    hook = random.choice(hooks)
    trimmed = summary.replace("\n", " ").strip()
    if len(trimmed) > max_chars:
        trimmed = trimmed[:max_chars].rsplit(" ", 1)[0] + "..."
    cta = "Follow for more daily facts."
    return f"{hook} {trimmed} {cta}"

async def generate_voice(script_text, out_path):
    try:
        communicate = edge_tts.Communicate(script_text, voice="en-US-ChristopherNeural")
        await communicate.save(str(out_path))
    except Exception as e:
        logger.exception("edge_tts failed: %s", e)
        raise

def fetch_commons_images(topic, max_images=NUM_IMAGES, width=1280):
    safe_topic = requests.utils.quote(topic)
    url = (
        "https://commons.wikimedia.org/w/api.php?"
        f"action=query&generator=search&gsrnamespace=6&gsrsearch={safe_topic}"
        "&prop=imageinfo&iiprop=url|extmetadata&iiurlwidth=1280&format=json"
    )
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning("Wikimedia query failed: %s", e)
        data = {}

    images = []
    pages = data.get("query", {}).get("pages", {})
    for pid, page in pages.items():
        if "imageinfo" in page and page["imageinfo"]:
            info = page["imageinfo"][0]
            img_url = info.get("thumburl") or info.get("url")
            if img_url:
                try:
                    img_data = requests.get(img_url, timeout=20).content
                    fname = WORKDIR / f"img{len(images)}.jpg"
                    with open(fname, "wb") as fh:
                        fh.write(img_data)
                    images.append(str(fname))
                    if len(images) >= max_images:
                        break
                except Exception as e:
                    logger.debug("Failed to download image %s: %s", img_url, e)
                    continue
    while len(images) < max_images:
        fname = WORKDIR / f"img{len(images)}.jpg"
        img = Image.new("RGB", VIDEO_RES, color=(random.randint(20,200), random.randint(20,200), random.randint(20,200)))
        img.save(fname)
        images.append(str(fname))
    return images

def create_thumbnail(base_image_path, title, out_path=THUMB_FILE, res=VIDEO_RES):
    img = Image.open(base_image_path).convert("RGB").resize(res)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(FONT_PATH, 80)
    except Exception:
        font = ImageFont.load_default()
    wrapped = textwrap.fill(title.upper(), width=18)
    w, h = draw.multiline_textsize(wrapped, font=font)
    x = res[0] // 2
    y = res[1] // 2 - h // 2
    draw.multiline_text((x, y), wrapped, font=font, fill="yellow", stroke_width=6, stroke_fill="black", anchor="mm", align="center")
    img.save(out_path)

def build_ffmpeg_cmd(image_files, voice_file, out_file=VIDEO_FILE, seconds_per=SECONDS_PER_IMAGE, framerate=FRAMERATE):
    cmd = ["ffmpeg", "-y"]
    for img in image_files:
        cmd += ["-loop", "1", "-framerate", str(framerate), "-t", str(seconds_per), "-i", img]
    cmd += ["-i", str(voice_file)]
    vf_parts = []
    num_imgs = len(image_files)
    for i in range(num_imgs):
        vf_parts.append(f"[{i}:v]scale={VIDEO_RES[0]}:{VIDEO_RES[1]},setsar=1,fps={framerate}[v{i}]")
    concat_inputs = "".join(f"[v{i}]" for i in range(num_imgs))
    vf_parts.append(f"{concat_inputs}concat=n={num_imgs}:v=1:a=0,format=yuv420p[vout]")
    filter_complex = ";".join(vf_parts)
    cmd += ["-filter_complex", filter_complex, "-map", "[vout]", "-map", f"{num_imgs}:a", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(out_file)]
    return cmd

def upload_to_youtube(video_path, thumb_path, topic, script):
    creds = Credentials(
        token=None,
        refresh_token=REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/youtube.upload"]
    )
    youtube = build("youtube", "v3", credentials=creds)
    title = f"Crazy Facts About {topic}"
    description = f"{script}\n\n🔗 Related: https://www.amazon.com/s?k={topic.replace(' ', '+')}&tag={AMAZON_ID}"
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": [topic, "facts", "education", "did you know"],
            "categoryId": "28"
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False
        }
    }
    media = MediaFileUpload(str(video_path), resumable=True)
    try:
        req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        resp = req.execute()
        vid = resp.get("id")
        if thumb_path and os.path.exists(thumb_path):
            youtube.thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(thumb_path))).execute()
        return vid
    except Exception as e:
        logger.exception("YouTube upload failed: %s", e)
        raise

# ---------- Main ----------
def main():
    try:
        logger.info("Starting run")
        page = safe_wikipedia_page()
        topic = page.title
        summary = page.summary[:800]
        script = build_script(topic, summary)
        logger.info("Topic chosen: %s", topic)

        logger.info("Generating voiceover")
        asyncio.run(generate_voice(script, VOICE_FILE))

        logger.info("Fetching images")
        images = fetch_commons_images(topic)

        logger.info("Creating thumbnail")
        create_thumbnail(images[0], topic, THUMB_FILE)

        logger.info("Building video with ffmpeg")
        ff_cmd = build_ffmpeg_cmd(images, VOICE_FILE, VIDEO_FILE)
        logger.info("Running ffmpeg: %s", " ".join(ff_cmd[:6]) + " ...")
        subprocess.run(ff_cmd, check=True)

        logger.info("Uploading to YouTube")
        video_id = upload_to_youtube(VIDEO_FILE, THUMB_FILE, topic, script)
        logger.info("Upload complete: %s", video_id)
        print("Video uploaded! ID:", video_id)
    except Exception as e:
        logger.exception("Run failed: %s", e)
        print("Run failed:", e)
        sys.exit(1)

if __name__ == "__main__":
    main()
