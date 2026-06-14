import wikipedia
import edge_tts
import asyncio
import requests
import os
import subprocess
from PIL import Image, ImageDraw, ImageFont
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileAPI

# ==========================================
# PASTE YOUR NOTEPAD KEYS INSIDE THE QUOTES BELOW
# ==========================================
AMAZON_ID = "autonomousaff-20"
CLIENT_ID = "827724330470-cj8bliafer1qhmfcnee4lt29dj6h0nnr.apps.googleusercontent.com"
CLIENT_SECRET = "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"
REFRESH_TOKEN = "1//04Tm97nLD0ovICgYIARAAGAQSNwF-L9IraDzGLdBNkqdmf6WtQQFVhxd24MdjwKlbzuJ4MkHzFf0uOWXXR2xDEoxXzt6gFSkb9lk"
# ==========================================

print("1. Finding a random topic...")
# Get a random Wikipedia page
wiki_page = wikipedia.page(wikipedia.random(pages=1))
topic = wiki_page.title
summary = wiki_page.summary[:400] # Grab first 400 characters
script = f"Did you know that {topic}? Here are some crazy facts. {summary} Subscribe for more daily facts!"
print(f"Topic chosen: {topic}")

print("2. Generating voiceover...")
async def generate_audio():
    # Use Microsoft Edge's free, unlimited neural voice
    communicate = edge_tts.Communicate(script, voice="en-US-ChristopherNeural")
    await communicate.save("voice.mp3")
asyncio.run(generate_audio())

print("3. Downloading free public images...")
# Query Wikimedia Commons (100% free, no API key, no limits)
search_term = topic.replace(" ", "%20")
url = f"https://commons.wikimedia.org/w/api.php?action=query&generator=search&gsrnamespace=6&gsrsearch={search_term}&prop=imageinfo&iiprop=url&iiurlwidth1280&format=json"
response = requests.get(url).json()

images_found = 0
if 'query' in response and 'pages' in response['query']:
    for page_id, page_data in response['query']['pages'].items():
        if images_found >= 5:
            break
        if 'imageinfo' in page_data and len(page_data['imageinfo']) > 0:
            img_url = page_data['imageinfo'][0]['thumburl']
            img_data = requests.get(img_url).content
            with open(f'img{images_found}.jpg', 'wb') as handler:
                handler.write(img_data)
            images_found += 1

# Fallback: If Wikimedia doesn't have 5 images, create blank colored images so the code doesn't crash
while images_found < 5:
    img = Image.new('RGB', (1280, 720), color = (73, 109, 137))
    img.save(f'img{images_found}.jpg')
    images_found += 1

print("4. Creating thumbnail...")
# Open the first image and draw massive text on it
img = Image.open('img0.jpg').convert('RGB').resize((1280, 720))
draw = ImageDraw.Draw(img)
try:
    # Try to use a bold font, fallback to default if not found on Linux server
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 80)
except:
    font = ImageFont.load_default()

# Draw text in the center, yellow with black outline
draw.text((640, 360), topic.upper(), font=font, fill="yellow", stroke_width=6, stroke_fill="black", anchor="mm")
img.save("thumb.jpg")

print("5. Editing video with FFmpeg...")
# Stitch 5 images (3 seconds each = 15 seconds) with the voiceover
ffmpeg_cmd = [
    "ffmpeg", "-y",
    "-loop", "1", "-t", "3", "-i", "img0.jpg",
    "-loop", "1", "-t", "3", "-i", "img1.jpg",
    "-loop", "1", "-t", "3", "-i", "img2.jpg",
    "-loop", "1", "-t", "3", "-i", "img3.jpg",
    "-loop", "1", "-t", "3", "-i", "img4.jpg",
    "-i", "voice.mp3",
    "-filter_complex", "[0:v][1:v][2:v][3:v][4:v]concat=n=5:v=1:a=0,format=yuv420p[v]",
    "-map", "[v]", "-map", "5:a",
    "-c:v", "libx264", "-c:a", "aac", "-shortest", "final_video.mp4"
]
subprocess.run(ffmpeg_cmd, check=True)

print("6. Uploading to YouTube...")
# Authenticate using the permanent Refresh Token
creds = Credentials(
    token=None,
    refresh_token=REFRESH_TOKEN
    token_uri='https://oauth2.googleapis.com/token',
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    scopes=['https://www.googleapis.com/auth/youtube']
)
youtube = build('youtube', 'v3', credentials=creds)

# Upload the video
video_request = youtube.videos().insert(
    part="snippet,status",
    body={
        "snippet": {
            "title": f"Crazy Facts About {topic}",
            "description": f"Learn about {topic}!\n\n🔗 Check out related items on Amazon: https://www.amazon.com/s?k={topic.replace(' ', '+')}&tag={AMAZON_ID}",
            "tags": [topic, "facts", "education", "did you know"],
            "categoryId": "28"
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False
        }
    },
    media_body=MediaFileUpload("final_video.mp4")
)
video_response = video_request.execute()
video_id = video_response['id']
print(f"Video uploaded! ID: {video_id}")

# Upload the thumbnail
youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload("thumb.jpg")).execute()
print("Thumbnail set! Robot finished successfully.")
