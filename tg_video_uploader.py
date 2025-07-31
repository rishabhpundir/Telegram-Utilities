#!/usr/bin/env python3
"""
tg_video_uploader.py – Batch video uploader with TXT-manifest support
-------------------------------------------------------
• Reads manifest.txt     :  FORMAT PER LINE--> URL, Optional custom title
• Uploads with Telethon  :  preserves your existing processing logic
• Logs to logs/output_*.txt
"""

import os
import re
import sys
import csv
import asyncio
import aiohttp
import aiofiles
import traceback
import subprocess 
import datetime as dt
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import RPCError, FloodWaitError

# ──────────────────────────  CONFIG  ────────────────────────── #
load_dotenv()
API_ID       = int(os.getenv("API_ID"))
API_HASH     = os.getenv("API_HASH")
BOT_TOKEN    = os.getenv("BOT_TOKEN")
CHANNEL_ID   = int(os.getenv("CHANNEL_ID"))                      # chat ID or @username

MANIFEST_FILE = Path("manifest.txt")                           # default path
LOG_DIR       = Path("logs")
DOWNLOAD_DIR  = Path("videos")                                 # temp downloads
TIMEOUT_SEC   = 600                                            # per-video timeout

URL_RE = re.compile(r'https?://', re.I)

# ───────────────────────  HELPER FUNCTIONS  ─────────────────── #

def timestamp(fmt="%H%M%S_%d%m%Y") -> str:
    return dt.datetime.now().strftime(fmt)


def get_resolution(path: Path) -> str | None:
    """
    Returns WxH string by asking ffprobe (must have ffmpeg installed).
    """
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=s=x:p=0", str(path)
        ]
        out = subprocess.check_output(cmd, text=True).strip()
        return out or None          # e.g. "1920x1080"
    except Exception:
        return None


def parse_manifest(path: Path):
    """
    Each line:  URL , Optional Title
    Blank title (or no comma) ⇒ None
    """
    videos = []
    with path.open(newline='', encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            url = row[0].strip()
            if not URL_RE.match(url):
                continue                                # ignore stray lines
            title = row[1].strip() if len(row) > 1 and row[1].strip() else None
            videos.append((url, title))
    return videos


VALID_EXTS = (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".flv")

async def _direct_http_download(url: str) -> Path:
    filename = url.split("?")[0].split("/")[-1]           # crude but works if ext is present
    dest = DOWNLOAD_DIR / filename
    async with aiohttp.ClientSession() as session, \
               session.get(url, timeout=TIMEOUT_SEC) as resp, \
               aiofiles.open(dest, "wb") as f:
        resp.raise_for_status()
        async for chunk in resp.content.iter_chunked(1 << 16):
            await f.write(chunk)
    return dest


import requests, uuid
async def download_video(url: str) -> Path:
    """
    1. Try yt-dlp (with force_generic_extractor).
    2. If yt-dlp bails (eg. weird .php link), fall back to a simple
       streamed HTTP download with live progress.
    """
    from yt_dlp import YoutubeDL
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    loop = asyncio.get_running_loop()

    # ── yt-dlp live-progress ──────────────────────────────────── #
    def _progress_hook(d):
        if d["status"] == "downloading":
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 1
            cur = d.get("downloaded_bytes", 0)
            pct = cur / tot * 100
            sys.stdout.write(f"\r⬇️  Downloading: {pct:6.2f}% ({cur/1_048_576:,.1f} MB)")
            sys.stdout.flush()
        elif d["status"] == "finished":
            print("\n✅ Download complete (yt-dlp)")

    def _ydl_blocking():
        ydl_opts = {
            "outtmpl": str(DOWNLOAD_DIR / "%(title).200s.%(ext)s"),
            "format": "bestvideo+bestaudio/best",
            "quiet": True,
            "merge_output_format": "mp4",
            "progress_hooks": [_progress_hook],
            "force_generic_extractor": True,          # ← keeps going on .php etc.
        }
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return Path(ydl.prepare_filename(info)).with_suffix(".mp4")

    # ── fallback: plain HTTP download ─────────────────────────── #
    def _direct_blocking():
        local = DOWNLOAD_DIR / f"{uuid.uuid4().hex}.mp4"
        with requests.get(url, stream=True, timeout=15) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            downloaded = 0
            chunk = 256 * 1024          # 256 kB
            with local.open("wb") as f:
                for data in r.iter_content(chunk):
                    f.write(data)
                    downloaded += len(data)
                    pct = downloaded / total * 100 if total else 0
                    sys.stdout.write(f"\r⬇️  Downloading (direct): {pct:6.2f}% ({downloaded/1_048_576:,.1f} MB)")
                    sys.stdout.flush()
        print("\n✅ Download complete (direct)")
        return local

    # ── try yt-dlp first, else fallback ───────────────────────── #
    try:
        return await loop.run_in_executor(None, _ydl_blocking)
    except Exception as e:
        print(f"\n⚠️ yt-dlp failed → {e}. Falling back to direct HTTP download…")
        return await loop.run_in_executor(None, _direct_blocking)




def build_caption(file_path: Path, custom_title: str | None = None) -> str:
    size_mb = round(file_path.stat().st_size / 1_048_576, 1)
    name    = file_path.stem
    res     = get_resolution(file_path) or "—"

    parts: list[str] = []
    if custom_title:
        parts.append(f"📺 {custom_title}")

    parts += [
        f"Name: {name}",
        f"Size: {size_mb} MB",
        f"Resolution: {res}",
        f"Uploaded: {dt.datetime.now():%d %b %Y}",
    ]
    return "\n".join(parts)


def upload_progress(cur: int, tot: int):
    pct = cur / tot * 100 if tot else 0
    sys.stdout.write(f"\r⬆️  Uploading:  {pct:6.2f}%  ({cur/1_048_576:,.1f} MB)")
    sys.stdout.flush()
    if cur == tot:
        print("\n✅ Upload complete")


async def process_video(client: TelegramClient,
                        url: str,
                        title: str | None,
                        logfile):
    video_path = None                                   # ← initialise
    try:
        video_path = await asyncio.wait_for(download_video(url),
                                             timeout=TIMEOUT_SEC)
        caption = build_caption(video_path, title)
        await client.send_file(
            CHANNEL_ID, 
            video_path, 
            caption=caption,
            progress_callback=upload_progress,
            supports_streaming=True,
            thumb=video_path,
        )
        logfile.write(f"SUCCESS,{url},{title or ''}\n")
        logfile.flush()                                 # ensure line is written
    except asyncio.TimeoutError:
        logfile.write(f"TIMEOUT,{url},{title or ''}\n")
        logfile.flush()
    except (FloodWaitError, RPCError) as tg_err:
        logfile.write(f"FAILED,{url},{title or ''},Telegram:{tg_err}\n")
        logfile.flush()
    except Exception as e:
        logfile.write(f"FAILED,{url},{title or ''},Other:{e}\n")
        logfile.flush()
    finally:
        # optional cleanup – only if we actually downloaded something
        if video_path and video_path.exists():
            try:
                video_path.unlink()
            except Exception:
                pass


# ─────────────────────────────  MAIN  ───────────────────────── #

async def main():
    try:
        if not MANIFEST_FILE.exists():
            sys.exit(f"❌ Manifest file not found: {MANIFEST_FILE}")

        videos = parse_manifest(MANIFEST_FILE)
        if not videos:
            sys.exit("❌ No valid URLs found in manifest.")

        LOG_DIR.mkdir(exist_ok=True)
        log_path = LOG_DIR / f"output_{timestamp()}.txt"

        client = TelegramClient("uploader_session", API_ID, API_HASH)
        await client.start(bot_token=BOT_TOKEN)

        print(f"▶️  Starting upload of {len(videos)} video(s)…")
        with log_path.open("w", encoding="utf-8") as log:
            for url, title in videos:
                await process_video(client, url, title, log)

        await client.disconnect()
        print(f"✅ Done. Detailed run log: {log_path.resolve()}")
    except Exception as e:
        print("Error: \n", traceback.format_exc())
    finally:
        os.removedirs(DOWNLOAD_DIR)


if __name__ == "__main__":
    asyncio.run(main())

