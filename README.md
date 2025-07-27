Telegram Utilities 

Two companion scripts automate common Telegram tasks:
Script	                Purpose	                Typical Run
tg_video_uploader.py	Batch-download videos from a list of URLs ( manifest.txt ) and upload them to a channel / bot with live progress bars and automatic captions	python tg_video_uploader.py
tg_chat_backup.py	    Incrementally export every message (optionally media) from one chat and re-post it to an archive channel, safely resuming if interrupted	python tg_chat_backup.py


1 . Features
tg_video_uploader.py
    Reads a simple comma-separated manifest: URL,Optional Custom Title
    Dual downloader: tries yt-dlp first (handles YouTube, HLS, redirects, etc.); falls back to a plain HTTP stream if that fails.
    Real-time download % and upload % meters in the console.
    Caption automatically lists file name, size, resolution (via ffprobe) and date.
    Logs every run to logs/output_<HHMMSS_DDMMYYYY>.txt for success/timeout/failure tracking.
    Cleans up local files once the upload finishes.

tg_chat_backup.py
    Uses Telethon’s high-level iterator to walk through the source chat, resuming from the last saved message ID so you can stop/start any time.
    Sends messages in configurable batches (BATCH_SIZE = 20) with randomized delays to avoid flood limits.
    Detects and re-uploads attached media, live locations, and webpage previews.
    Writes structured progress and error details to backup.log (console + file logger).
    All credentials are pulled from environment variables, keeping secrets out of the codebase.


2 . Requirements
Category	Details
Python	3.10 or newer (both scripts use modern type hints & `
Core packages	telethon, python-dotenv, pytz, aiohttp, aiofiles, requests, yt-dlp
System tools	ffmpeg/ffprobe in PATH (needed for video-resolution metadata)
Optional	GNU make or similar for shortcut commands

Create a requirements.txt with the packages above and run pip install -r requirements.txt.


3 . Configuration (.env)
Both scripts expect a .env in the project root:

# Telegram API (get from https://my.telegram.org/apps)
API_ID=123456
API_HASH=0123456789abcdef0123456789abcdef

# Bot token (BotFather) – only required if you run scripts via a bot account
BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11

# Destination / Source IDs
CHANNEL_ID=-1001234567890        # where videos are uploaded
CHAT_ID=-1009876543210           # source chat to back up
DESTINATION_CHANNEL_ID=-1001122334455   # archive chat for backups

Tip: IDs for channels/chats start with -100…. You can get them from Telethon (get_entity) or @userinfobot.


4 . Installation & First Run
git clone https://github.com/youruser/telegram-utils.git
cd telegram-utils

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Mac / Linux: install ffmpeg (e.g. brew install ffmpeg) 
# Windows    : choco install ffmpeg

# Put your credentials in .env
cp .env.example .env   # then edit

# Prepare folders
mkdir -p logs videos

# Create your manifest
echo "https://example.com/clip.mp4,My Intro Clip" > manifest.txt

Run:
python tg_video_uploader.py      # uploads everything in manifest.txt
python tg_chat_backup.py         # starts / resumes the backup loop
On first launch Telethon will open a login prompt in the console to authorise the session; subsequent runs reuse the saved session file (uploader_session.session / Chat_Backup.session).


5 . Manifest Format (tg_video_uploader.py)
URL,Custom Title   ← title optional; leave blank or omit comma for defaults
https://files.foo/abc123.mp4,Spring Promo
https://cdn.bar/latest.php,                         ← no title
https://vimeo.com/123456789,Behind the Scenes
