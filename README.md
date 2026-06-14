# auto-yt — Fully automated YouTube Shorts pipeline (100% free tools)

This repository runs a daily automated pipeline that:
- picks a topic from Wikipedia,
- generates a natural voiceover (edge-tts with pyttsx3 fallback),
- sources motion clips from Internet Archive (public domain) or Commons images,
- builds a cinematic vertical Short with FFmpeg,
- uploads to YouTube with affiliate links and license attributions,
- runs on GitHub Actions daily.

## Quick setup (no coding experience required)

1. Create a **public** GitHub repository and push these files.
2. Add GitHub Secrets (Repository → Settings → Secrets and variables → Actions):
   - `AMAZON_ID` — your Amazon Associates tracking id (e.g., yourname-20)
   - `CLIENT_ID` — Google OAuth client id
   - `CLIENT_SECRET` — Google OAuth client secret
   - `REFRESH_TOKEN` — refresh token from OAuth Playground (YouTube Data API scope)
3. Commit files and open **Actions** → **Daily Auto Upload** → **Run workflow**.
4. Check logs; artifacts (video, thumb, meta, log) are uploaded after each run.

## Notes and safety
- **Do not commit secrets** into the repo. Use GitHub Secrets.
- The script records license metadata for clips; review before monetizing.
- If you want higher-quality local TTS or captions, see optional upgrades below.

## Optional upgrades (free, advanced)
- **Coqui TTS** (local, better voice): install locally or on a self-hosted runner; instructions below.
- **whisper.cpp** (local captions): build with `build_whisper.sh` and integrate to auto-generate SRT captions.
- **Batch mode**: modify `main.py` to generate multiple videos and schedule one upload per day.

## Troubleshooting
- FFmpeg errors: ensure `ffmpeg` installed in workflow step succeeded.
- YouTube auth errors: verify `CLIENT_ID`, `CLIENT_SECRET`, `REFRESH_TOKEN`.
- Commons/IA download errors: transient network issues; re-run workflow.

## Optional helper: build whisper.cpp
Run `bash build_whisper.sh` on a Linux machine to build whisper.cpp for local transcription.
