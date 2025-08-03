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
import uuid
import shutil
import ffmpeg
import asyncio
import aiohttp
import aiofiles
import requests
import traceback
import subprocess 
import datetime as dt

from pathlib import Path
from yt_dlp import YoutubeDL
from dotenv import load_dotenv
from telethon import TelegramClient
from moviepy import VideoFileClip
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
VALID_EXTS = (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".flv")

# ───────────────────────  HELPER FUNCTIONS  ─────────────────── #

def timestamp(fmt="%H%M%S_%d%m%Y") -> str:
    return dt.datetime.now().strftime(fmt)


def get_media_info(path: Path) -> tuple[str, str] | None:
    """
    Returns (resolution, runtime) → ("1920x1080", "HH:MM:SS")
    """
    try:
        # ── resolution ────────────────────────────────────────────
        res = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=s=x:p=0", str(path),
            ],
            text=True,
        ).strip()

        # ── duration (in seconds) ─────────────────────────────────
        dur_sec = float(
            subprocess.check_output(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                text=True,
            ).strip()
        )
        h, m = divmod(int(dur_sec + 0.5), 3600)
        m, s = divmod(m, 60)
        runtime = f"{h:02d}:{m:02d}:{s:02d}"

        return res, runtime
    except Exception:
        return None


def parse_manifest(path: Path):
    """
    Each line: URL, Optional Title, Optional Thumb Timestamp (HH:MM:SS), Optional End Length (HH:MM:SS)
    """
    videos = []
    with path.open(newline='', encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            url = row[0].strip()
            if not URL_RE.match(url):
                continue
            title = row[1].strip() if len(row) > 1 and row[1].strip() else None
            thumb_ts = row[2].strip() if len(row) > 2 and row[2].strip() else None
            end_ts = row[3].strip() if len(row) > 3 and row[3].strip() else None
            videos.append((url, title, thumb_ts, end_ts))
    return videos


def extract_thumb_and_trim(input_path: Path, thumb_ts: str | None, end_ts: str | None) -> tuple[Path, Path]:
    """
    Returns: (new_video_path, thumb_path)
    - Trims the video if end_ts is provided.
    - Extracts thumbnail at thumb_ts or midpoint if not given.
    """

    thumb_path = input_path.with_name(f"{input_path.stem}_thumb.jpg")
    final_video_path = input_path

    # Trim video if end_ts given
    if end_ts:
        final_video_path = input_path.with_name(f"{input_path.stem}_trimmed.mp4")
        ffmpeg.input(str(input_path), t=end_ts).output(str(final_video_path), c="copy").overwrite_output().run()
    
    # Extract midpoint if thumb_ts not given
    if not thumb_ts:
        with VideoFileClip(str(final_video_path)) as clip:
            mid_time = clip.duration / 2
            thumb_ts = str(dt.timedelta(seconds=int(mid_time)))
            
    ffmpeg.input(str(final_video_path), ss=thumb_ts).output(
        str(thumb_path),
        vframes=1,
        vcodec="mjpeg",
        vf="scale='min(iw\\,320)':-2",
        **{"qscale:v": "2", "strict": "-2"},
        format="image2",
        loglevel="error"
    ).overwrite_output().run()

    return final_video_path, thumb_path


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


async def download_video(url: str) -> Path:
    """
    1. Try yt-dlp (with force_generic_extractor).
    2. If yt-dlp bails (eg. weird .php link), fall back to a simple
       streamed HTTP download with live progress.
    """
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
    name = file_path.stem
    res, runtime = get_media_info(file_path) or "—"

    parts: list[str] = []
    if custom_title:
        parts.append(f"<b>▶️ {custom_title.title()}</b>\n")

    parts += [
        # f"Name: {name}",
        "—" * 11,
        f"Quality: {res}p",
        f"Length: {runtime.replace(":", "꞉")}",
        f"File Size: {size_mb}MB",
        f"Added: {dt.datetime.now():%d-%m-%Y}",
        "—" * 11,
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
                        thumb_ts: str | None,
                        end_ts: str | None,
                        logfile):
    video_path = None
    try:
        video_path = await asyncio.wait_for(download_video(url), timeout=TIMEOUT_SEC)
        processed_video, thumb_path = extract_thumb_and_trim(video_path, thumb_ts, end_ts)

        caption = build_caption(processed_video, title)
        await client.send_file(
            CHANNEL_ID, 
            processed_video, 
            caption=f"<i>{caption}</i>",
            progress_callback=upload_progress,
            supports_streaming=True,
            thumb=thumb_path,
            parse_mode="html"
        )
        logfile.write(f"SUCCESS,{url},{title or ''}\n")
        logfile.flush()
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
            for index, (url, title, thumb_ts, end_ts) in enumerate(videos, 1):
                print("-" * 50)
                print(f"#{index}. Processing : {title}")
                await process_video(client, url, title, thumb_ts, end_ts, log)


        await client.disconnect()
        print(f"✅ Done. Detailed run log: {log_path.resolve()}")
    except Exception as e:
        print("Error: \n", traceback.format_exc())
    finally:
        shutil.rmtree(DOWNLOAD_DIR)


if __name__ == "__main__":
    asyncio.run(main())

