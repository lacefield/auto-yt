#!/usr/bin/env python3
"""
main.py
Secure, robust, fully automated YouTube Shorts pipeline.
- Reads credentials from environment variables: AMAZON_ID, CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN
- Sources topics from Wikipedia (robust), Internet Archive (public-domain videos), Wikimedia Commons
- Uses edge-tts (preferred) with pyttsx3 fallback for offline TTS
- Builds cinematic video using ffmpeg (called via subprocess)
- Generates thumbnails (Pillow) with backward-compatible text sizing
- Uploads to YouTube via googleapiclient
- Saves run metadata and artifacts for debugging
"""

from pathlib import Path
import os
import sys
import time
import json
import random
import logging
import asyncio
import textwrap
import subprocess
from datetime import datetime

import requests
import wikipedia
import edge_tts
from PIL import Image, ImageDraw, ImageFont

# Optional offline TTS fallback
try:
    import pyttsx3
except Exception:
    pyttsx3 = None

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ---------- Config ----------
WORKDIR = Path.cwd()
DOWNLOADS = WORKDIR / "downloads"
ARTIFACTS = WORKDIR / "artifacts"
VOICE_FILE = WORKDIR / "voice.mp3"
VIDEO_FILE = WORKDIR / "final_video.mp4"
THUMB_FILE = WORKDIR / "thumb.jpg"
META_FILE = WORKDIR / "meta.json"

NUM_CLIPS = 4
CLIP_DURATION = 6            # seconds per clip
FPS = 30
VIDEO_RES = (1080, 1920)     # vertical Shorts by default
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
WIKIMEDIA_IIWIDTH = 1280
WIKIMEDIA_MAX_RESULTS = 12
IA_MAX_RESULTS = 12
WIKIPEDIA_MIN_SUMMARY = 80
FALLBACK_TOPICS = [
    "Space exploration", "Ancient Egypt", "Great Barrier Reef",
    "Mount Everest", "Electricity", "Leonardo da Vinci",
    "Black holes", "Photosynthesis", "Antarctica", "Volcano"
]
# ----------------------------

# ---------- Logging ----------
os.makedirs(DOWNLOADS, exist_ok=True)
os.makedirs(ARTIFACTS, exist_ok=True)
logging.basicConfig(
    filename=str(WORKDIR / "robot.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("auto-yt")
# ----------------------------

# ---------- Secrets (from env) ----------
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

# ---------- Utilities ----------
def multiline_text_size(draw, text, font, spacing=4):
    if hasattr(draw, "multiline_textbbox"):
        bbox = draw.multiline_textbbox((0, 0), text, font=font, spacing=spacing)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]
    lines = text.splitlines()
    widths, heights = [], []
    for line in lines:
        w, h = draw.textsize(line, font=font)
        widths.append(w); heights.append(h)
    total_h = sum(heights) + spacing * (len(lines) - 1)
    return max(widths) if widths else 0, total_h

def safe_wikipedia_page(max_attempts=30, min_summary_len=WIKIPEDIA_MIN_SUMMARY):
    from wikipedia import DisambiguationError, PageError
    attempt = 0
    backoff = 1.0
    while attempt < max_attempts:
        attempt += 1
        try:
            title = wikipedia.random(1)
            page = wikipedia.page(title, auto_suggest=False)
            summary = (page.summary or "").strip()
            if len(summary) >= min_summary_len:
                return page
        except DisambiguationError as e:
            try:
                choice = random.choice(e.options)
                page = wikipedia.page(choice, auto_suggest=False)
                if len((page.summary or "").strip()) >= min_summary_len:
                    return page
            except Exception:
                pass
        except PageError:
            pass
        except Exception:
            logger.debug("wikipedia.random failed on attempt %d", attempt, exc_info=True)

        # fallback: search a curated topic
        try:
            q = random.choice(FALLBACK_TOPICS)
            results = wikipedia.search(q, results=10)
            random.shuffle(results)
            for candidate in results:
                try:
                    page = wikipedia.page(candidate, auto_suggest=False)
                    if len((page.summary or "").strip()) >= min_summary_len:
                        return page
                except Exception:
                    continue
        except Exception:
            logger.debug("wikipedia.search fallback failed", exc_info=True)

        time.sleep(backoff)
        backoff = min(backoff * 1.8, 30.0)

    raise RuntimeError("Could not fetch a suitable Wikipedia page after multiple attempts.")

# ---------- TTS ----------
async def generate_voice_edge(script_text, out_path):
    communicate = edge_tts.Communicate(script_text, voice="en-US-ChristopherNeural")
    await communicate.save(str(out_path))

def generate_voice_pyttsx3(script_text, out_path):
    if pyttsx3 is None:
        raise RuntimeError("pyttsx3 not available for offline TTS fallback.")
    engine = pyttsx3.init()
    engine.setProperty("rate", 150)
    engine.save_to_file(script_text, str(out_path))
    engine.runAndWait()

def generate_voice(script_text, out_path):
    try:
        logger.info("Generating voice with edge-tts")
        asyncio.run(generate_voice_edge(script_text, out_path))
        logger.info("edge-tts saved to %s", out_path)
    except Exception:
        logger.exception("edge-tts failed, trying pyttsx3 fallback")
        generate_voice_pyttsx3(script_text, out_path)
        logger.info("pyttsx3 saved to %s", out_path)

# ---------- Wikimedia Commons images (fallback motion) ----------
def fetch_commons_images(topic, max_images=5, width=WIKIMEDIA_IIWIDTH):
    safe_topic = requests.utils.quote(topic)
    url = (
        "https://commons.wikimedia.org/w/api.php?"
        f"action=query&generator=search&gsrnamespace=6&gsrsearch={safe_topic}"
        f"&gsrlimit={WIKIMEDIA_MAX_RESULTS}&prop=imageinfo&iiprop=url|extmetadata&iiurlwidth={width}&format=json"
    )
    images = []
    try:
        r = requests.get(url, timeout=20); r.raise_for_status()
        data = r.json()
        pages = data.get("query", {}).get("pages", {})
        for pid, page in pages.items():
            if "imageinfo" in page and page["imageinfo"]:
                info = page["imageinfo"][0]
                img_url = info.get("thumburl") or info.get("url")
                if img_url:
                    try:
                        fname = DOWNLOADS / f"img_{len(images)}.jpg"
                        with requests.get(img_url, timeout=20, stream=True) as rr:
                            rr.raise_for_status()
                            with open(fname, "wb") as fh:
                                for chunk in rr.iter_content(1024*64):
                                    fh.write(chunk)
                        images.append(str(fname))
                        if len(images) >= max_images:
                            break
                    except Exception:
                        logger.debug("Failed to download image %s", img_url, exc_info=True)
                        continue
    except Exception:
        logger.exception("Wikimedia Commons query failed")

    while len(images) < max_images:
        fname = DOWNLOADS / f"img_{len(images)}.jpg"
        Image.new("RGB", VIDEO_RES, color=(random.randint(20,200), random.randint(20,200), random.randint(20,200))).save(fname)
        images.append(str(fname))
    return images

# ---------- Internet Archive video search ----------
def internet_archive_search_videos(query, max_results=IA_MAX_RESULTS):
    q = requests.utils.quote(query)
    url = f"https://archive.org/advancedsearch.php?q={q}+AND+mediatype:(movies OR movingimage)&fl[]=identifier,title&rows={max_results}&page=1&output=json"
    results = []
    try:
        r = requests.get(url, timeout=20); r.raise_for_status()
        data = r.json()
        for doc in data.get("response", {}).get("docs", []):
            ident = doc.get("identifier")
            if not ident:
                continue
            meta_url = f"https://archive.org/metadata/{ident}"
            try:
                mr = requests.get(meta_url, timeout=20).json()
                files = mr.get("files", [])
                for f in files:
                    name = f.get("name","")
                    if name.lower().endswith(('.mp4', '.webm', '.ogv', '.mov')):
                        file_url = f"https://archive.org/download/{ident}/{name}"
                        results.append({
                            "title": doc.get("title") or ident,
                            "url": file_url,
                            "license": f.get("license") or mr.get("metadata", {}).get("license"),
                            "credit": mr.get("metadata", {}).get("creator")
                        })
                        break
            except Exception:
                continue
    except Exception:
        logger.exception("Internet Archive search failed")
    return results

def download_with_retries(url, out_path, attempts=3):
    for i in range(attempts):
        try:
            with requests.get(url, stream=True, timeout=30) as r:
                r.raise_for_status()
                with open(out_path, "wb") as fh:
                    for chunk in r.iter_content(1024*64):
                        fh.write(chunk)
            return out_path
        except Exception as e:
            logger.warning("Download attempt %d failed for %s: %s", i+1, url, e)
            time.sleep(1 + i*2)
    raise RuntimeError(f"Failed to download {url}")

# ---------- Thumbnail ----------
def create_thumbnail(base_image_path, title, out_path=THUMB_FILE, res=VIDEO_RES):
    img = Image.open(base_image_path).convert("RGB").resize(res)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(FONT_PATH, 96)
    except Exception:
        font = ImageFont.load_default()
    text = title.upper()
    wrapped = textwrap.fill(text, width=14)
    spacing = 8
    w, h = multiline_text_size(draw, wrapped, font=font, spacing=spacing)
    x = res[0] // 2
    y = int(res[1] * 0.12)
    try:
        draw.multiline_text((x, y), wrapped, font=font, fill="yellow", stroke_width=6, stroke_fill="black", anchor="ma", align="center", spacing=spacing)
    except TypeError:
        lines = wrapped.splitlines()
        current_y = y
        for line in lines:
            lw, lh = draw.textsize(line, font=font)
            draw.text(((res[0] - lw) / 2, current_y), line, font=font, fill="yellow")
            current_y += lh + spacing
    img.save(out_path)

# ---------- FFmpeg video builder (clips or images) ----------
def build_ffmpeg_from_clips(clip_paths, voice_path, out_path=VIDEO_FILE, clip_duration=CLIP_DURATION, fps=FPS, res=VIDEO_RES):
    # Trim each clip to clip_duration and scale/crop to res using ffmpeg filter_complex
    inputs = []
    filter_parts = []
    for i, clip in enumerate(clip_paths):
        inputs += ["-ss", "0", "-t", str(clip_duration), "-i", clip]
        filter_parts.append(f"[{i}:v]scale={res[0]}:{res[1]},setsar=1,fps={fps}[v{i}]")
    inputs += ["-i", str(voice_path)]
    concat_inputs = "".join(f"[v{i}]" for i in range(len(clip_paths)))
    filter_parts.append(f"{concat_inputs}concat=n={len(clip_paths)}:v=1:a=0,format=yuv420p[vout]")
    filter_complex = ";".join(filter_parts)
    cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", filter_complex, "-map", "[vout]", "-map", f"{len(clip_paths)}:a", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(out_path)]
    logger.info("Running ffmpeg: %s", " ".join(cmd[:10]) + " ...")
    subprocess.run(cmd, check=True)
    return out_path

def build_ffmpeg_from_images(image_paths, voice_path, out_path=VIDEO_FILE, seconds_per=CLIP_DURATION, fps=FPS, res=VIDEO_RES):
    cmd = ["ffmpeg", "-y"]
    for img in image_paths:
        cmd += ["-loop", "1", "-framerate", str(fps), "-t", str(seconds_per), "-i", img]
    cmd += ["-i", str(voice_path)]
    vf_parts = []
    num_imgs = len(image_paths)
    for i in range(num_imgs):
        vf_parts.append(f"[{i}:v]scale={res[0]}:{res[1]},setsar=1,fps={fps}[v{i}]")
    concat_inputs = "".join(f"[v{i}]" for i in range(num_imgs))
    vf_parts.append(f"{concat_inputs}concat=n={num_imgs}:v=1:a=0,format=yuv420p[vout]")
    filter_complex = ";".join(vf_parts)
    cmd += ["-filter_complex", filter_complex, "-map", "[vout]", "-map", f"{num_imgs}:a", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(out_path)]
    logger.info("Running ffmpeg: %s", " ".join(cmd[:10]) + " ...")
    subprocess.run(cmd, check=True)
    return out_path

# ---------- YouTube upload ----------
def upload_to_youtube(video_path, thumb_path, topic, script, tags=None):
    if tags is None:
        tags = [topic, "shorts", "facts", "education"]
    creds = Credentials(
        token=None,
        refresh_token=REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/youtube.upload"]
    )
    youtube = build("youtube", "v3", credentials=creds)
    title = f"{topic} — Quick Facts"
    description = f"{script}\n\nRelated: https://www.amazon.com/s?k={topic.replace(' ', '+')}&tag={AMAZON_ID}"
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": "27"
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False
        }
    }
    media = MediaFileUpload(str(video_path), resumable=True)
    req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = req.execute()
    vid = resp.get("id")
    if thumb_path and os.path.exists(thumb_path):
        youtube.thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(thumb_path))).execute()
    return vid

# ---------- Main pipeline ----------
def build_script(title, summary, max_chars=600):
    hooks = [
        f"Stop scrolling. {title} is crazier than you think.",
        f"You think you know {title}? Wait until you hear this.",
        f"Quick facts about {title} that sound unreal."
    ]
    hook = random.choice(hooks)
    trimmed = summary.replace("\n", " ").strip()
    if len(trimmed) > max_chars:
        trimmed = trimmed[:max_chars].rsplit(" ", 1)[0] + "..."
    cta = random.choice(["Follow for more daily facts.", "Subscribe for more short mind-blowers."])
    return f"{hook} {trimmed} {cta}"

def run_once():
    logger.info("=== Run started ===")
    page = safe_wikipedia_page()
    topic = page.title
    summary = page.summary or ""
    script = build_script(topic, summary)
    logger.info("Topic: %s", topic)

    # 1) Generate voice
    logger.info("Generating voice")
    generate_voice(script, VOICE_FILE)

    # 2) Try Internet Archive for real motion clips
    logger.info("Searching Internet Archive for clips")
    ia_results = internet_archive_search_videos(topic)
    random.shuffle(ia_results)
    clip_paths = []
    clip_meta = []
    for i, item in enumerate(ia_results[:NUM_CLIPS*2]):
        if len(clip_paths) >= NUM_CLIPS:
            break
        ext = os.path.splitext(item["url"])[1] or ".mp4"
        outp = DOWNLOADS / f"clip_{i}{ext}"
        try:
            download_with_retries(item["url"], outp)
            clip_paths.append(str(outp))
            clip_meta.append({"title": item.get("title"), "url": item.get("url"), "license": item.get("license"), "credit": item.get("credit")})
        except Exception:
            continue

    # 3) If not enough clips, try Wikimedia Commons videos (rare) or fallback to images
    if len(clip_paths) < NUM_CLIPS:
        logger.info("Not enough IA clips, searching Commons images for motion fallback")
        images = fetch_commons_images(topic, max_images=NUM_CLIPS)
        # build video from images
        logger.info("Building video from images")
        build_ffmpeg_from_images(images, VOICE_FILE, VIDEO_FILE, seconds_per=CLIP_DURATION, fps=FPS, res=VIDEO_RES)
    else:
        # Trim/compose clips into final video
        logger.info("Building video from clips")
        build_ffmpeg_from_clips(clip_paths[:NUM_CLIPS], VOICE_FILE, VIDEO_FILE, clip_duration=CLIP_DURATION, fps=FPS, res=VIDEO_RES)

    # 4) Create thumbnail
    try:
        if clip_paths:
            # extract a frame from first clip
            frame_img = DOWNLOADS / "thumb_frame.jpg"
            subprocess.run(["ffmpeg", "-y", "-i", clip_paths[0], "-ss", "00:00:01.000", "-vframes", "1", str(frame_img)], check=False)
            base_img = str(frame_img) if frame_img.exists() else (images[0] if 'images' in locals() else None)
        else:
            base_img = images[0]
        if base_img:
            create_thumbnail(base_img, topic, THUMB_FILE, res=VIDEO_RES)
    except Exception:
        logger.exception("Thumbnail creation failed; using fallback")
        # fallback: create plain image
        Image.new("RGB", VIDEO_RES, color=(30,30,60)).save(THUMB_FILE)

    # 5) Upload
    logger.info("Uploading to YouTube")
    video_id = upload_to_youtube(VIDEO_FILE, THUMB_FILE, topic, script)
    logger.info("Uploaded video id: %s", video_id)

    # 6) Save metadata
    meta = {
        "timestamp": datetime.utcnow().isoformat(),
        "topic": topic,
        "script": script,
        "clips": clip_meta,
        "video_file": str(VIDEO_FILE),
        "thumb_file": str(THUMB_FILE),
        "video_id": video_id
    }
    with open(META_FILE, "w") as fh:
        json.dump(meta, fh, indent=2)
    logger.info("Run metadata saved to %s", META_FILE)
    logger.info("=== Run finished ===")
    print("Video uploaded! ID:", video_id)

if __name__ == "__main__":
    try:
        run_once()
    except Exception as e:
        logger.exception("Run failed: %s", e)
        print("Run failed:", e)
        sys.exit(1)
