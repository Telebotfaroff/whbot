import asyncio
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from providers.watchhentai import WatchHentai, ProviderError
from crawler.catalog import DB_PATH, init_db, crawl_series
from publisher import publish_pending, publish_ids, publish_series_ids
from gofile_uploader import GofileUploader

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
DOWNLOAD_CHANNEL_ID = os.getenv("DOWNLOAD_CHANNEL_ID")
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "7367490186") or 7367490186)

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError("API_ID, API_HASH and BOT_TOKEN are required")

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

PYROGRAM_WORKDIR = Path(os.getenv("PYROGRAM_WORKDIR", "./.pyrogram"))
PYROGRAM_WORKDIR.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
# Small pause before each Telegram media upload to reduce request bursts.
TELEGRAM_UPLOAD_DELAY = float(os.getenv("TELEGRAM_UPLOAD_DELAY", "3.0"))
TELEGRAM_PROGRESS_INTERVAL = max(float(os.getenv("TELEGRAM_PROGRESS_INTERVAL", "4.0")), 2.0)
# Pyrogram uploads files in parallel chunks. Increase this for better throughput.
# Keep it configurable because very high values can trigger Telegram flood control.
MAX_CONCURRENT_TRANSMISSIONS = max(int(os.getenv("MAX_CONCURRENT_TRANSMISSIONS", "4")), 1)
CALLBACK_URLS = {}
AUTO_LOCK = asyncio.Lock()
AUTO_STOP = threading.Event()
CRAWL_STATES = {}
DOWNLOAD_STATES = {}
CHANNEL_SETUP_USERS = set()

# Per-chat video merge sessions. Files remain on disk until the session is
# merged or cancelled. This is intentionally separate from the GoFile upload
# lifecycle so normal uploads can still clean up their temporary files.
MERGE_SESSIONS = {}
MERGE_LOCK = asyncio.Lock()
MAX_MERGE_VIDEOS = max(int(os.getenv("MAX_MERGE_VIDEOS", "100")), 2)
MAX_MERGE_FILE_BYTES = max(
    int(os.getenv("MAX_MERGE_FILE_GB", "20")), 1
) * 1024 * 1024 * 1024

app = Client(
    "whbot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=str(PYROGRAM_WORKDIR),
    parse_mode=ParseMode.HTML,
    max_concurrent_transmissions=MAX_CONCURRENT_TRANSMISSIONS,
)
provider = WatchHentai()


async def validate_download_channel():
    """Resolve and validate the configured Telegram download channel."""
    if not DOWNLOAD_CHANNEL_ID:
        print("[telegram] DOWNLOAD_CHANNEL_ID is not configured", flush=True)
        return False
    try:
        chat = await app.get_chat(DOWNLOAD_CHANNEL_ID)
        print(
            "[telegram] download channel OK: id={} title={} type={}".format(
                chat.id,
                getattr(chat, "title", None)
                or getattr(chat, "username", None)
                or "unknown",
                getattr(getattr(chat, "type", None), "value", None)
                or getattr(chat, "type", None)
                or "unknown",
            ),
            flush=True,
        )

        # Force Pyrogram's active bot session to resolve/cache the peer now.
        # This runs at startup as well as before upload jobs, preventing a
        # PEER_ID_INVALID caused by a peer that has not yet been resolved.
        peer = await app.resolve_peer(chat.id)
        print(
            "[telegram] Pyrogram peer resolved: id={} peer_type={}".format(
                chat.id, type(peer).__name__
            ),
            flush=True,
        )
        return True
    except Exception as exc:
        print(
            "[telegram] DOWNLOAD_CHANNEL_ID INVALID/INACCESSIBLE: {} ({})".format(
                DOWNLOAD_CHANNEL_ID, exc
            ),
            flush=True,
        )
        return False


async def initialize_telegram_peers():
    """Resolve Telegram peers immediately after the Pyrogram client starts."""
    print("[telegram] initializing Pyrogram peers...", flush=True)
    if not DOWNLOAD_CHANNEL_ID:
        print(
            "[telegram] no DOWNLOAD_CHANNEL_ID configured; startup peer resolution skipped",
            flush=True,
        )
        return

    ok = await validate_download_channel()
    if ok:
        print(
            "[telegram] startup channel peer resolution completed successfully",
            flush=True,
        )
    else:
        print(
            "[telegram] startup channel peer resolution FAILED; "
            "bot will remain online and user-chat fallback can be used",
            flush=True,
        )

# Background crawling is intentionally OFF by default.
CRAWL_INTERVAL = max(int(os.getenv("CRAWL_INTERVAL", "1800")), 60)
CRAWL_PAGES = max(int(os.getenv("CRAWL_PAGES", "1")), 1)
PUBLISH_LIMIT = max(int(os.getenv("PUBLISH_LIMIT", "20")), 1)
INITIAL_CRAWL_PAGES = max(int(os.getenv("INITIAL_CRAWL_PAGES", "0")), 0)
SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "false").lower() not in {"0", "false", "no", "off"}
BACKGROUND_LOCK = threading.Lock()


def enc(value):
    import hashlib
    token = hashlib.sha256(value.encode()).hexdigest()[:12]
    CALLBACK_URLS[token] = value

    # Persist callback mappings so buttons continue working after a Colab/bot
    # restart. The in-memory cache is still used for fast access.
    con = init_db()
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS callback_urls ("
            "token TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        con.execute(
            "INSERT OR REPLACE INTO callback_urls(token, value) VALUES (?, ?)",
            (token, value),
        )
        con.commit()
    finally:
        con.close()

    return token


def dec(value):
    if value in CALLBACK_URLS:
        return CALLBACK_URLS[value]

    con = init_db()
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS callback_urls ("
            "token TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        row = con.execute(
            "SELECT value FROM callback_urls WHERE token=?",
            (value,),
        ).fetchone()
    finally:
        con.close()

    if not row:
        raise RuntimeError("This button has expired. Open the episode again.")
    CALLBACK_URLS[value] = row[0]
    return row[0]


def format_duration(seconds):
    seconds = int(seconds or 0)
    if seconds <= 0:
        return "Unknown"
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def video_metadata(path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {"duration": 0, "width": 0, "height": 0}

    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height:format=duration",
                "-of", "json", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        data = json.loads(result.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
        fmt = data.get("format") or {}
        return {
            "duration": int(float(fmt.get("duration") or 0)),
            "width": int(stream.get("width") or 0),
            "height": int(stream.get("height") or 0),
        }
    except Exception:
        return {"duration": 0, "width": 0, "height": 0}


def load_download_channel_setting():
    """Load a previously verified download channel from SQLite."""
    global DOWNLOAD_CHANNEL_ID
    con = init_db()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS bot_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = con.execute("SELECT value FROM bot_settings WHERE key='download_channel_id'").fetchone()
        if row and row[0]:
            DOWNLOAD_CHANNEL_ID = row[0]
    finally:
        con.close()


def save_download_channel_setting(channel_id):
    """Persist a verified download channel across restarts."""
    global DOWNLOAD_CHANNEL_ID
    channel_id = str(channel_id)
    con = init_db()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS bot_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        con.execute("INSERT OR REPLACE INTO bot_settings(key, value) VALUES (?, ?)", ("download_channel_id", channel_id))
        con.commit()
    finally:
        con.close()
    DOWNLOAD_CHANNEL_ID = channel_id


def owner_only(message):
    return bool(BOT_OWNER_ID and message.from_user and message.from_user.id == BOT_OWNER_ID)


def db_rows(query, params=()):
    # Ensure the SQLite schema exists before any read. This is important on a
    # fresh Colab runtime where /latest may be the first database operation.
    con = init_db()
    try:
        return con.execute(query, params).fetchall()
    finally:
        con.close()


def mark_video_uploaded(post_id, message_id):
    con = init_db()
    try:
        con.execute(
            "UPDATE posts SET video_uploaded=1, video_message_id=? WHERE id=?",
            (message_id, post_id),
        )
        con.commit()
    finally:
        con.close()



def merge_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Add More Video", callback_data="merge:add"),
            InlineKeyboardButton("🔗 Merge Videos", callback_data="merge:run"),
        ],
        [
            InlineKeyboardButton("❌ Cancel Merge", callback_data="merge:cancel"),
        ],
    ])


def merge_status_text(session):
    files = session.get("files") or []
    total_bytes = sum(
        path.stat().st_size for path in files
        if path.exists()
    )
    return (
        "🎞️ <b>Video Merge Queue</b>\n\n"
        f"📹 Videos: <b>{len(files)}</b>\n"
        f"💾 Size: <b>{format_size(total_bytes)}</b>\n\n"
        "Send another video to add it to the queue, or press "
        "<b>Merge Videos</b> when you are finished."
    )


def _is_video_document(message):
    document = getattr(message, "document", None)
    if not document:
        return False
    mime = (getattr(document, "mime_type", None) or "").lower()
    name = (getattr(document, "file_name", None) or "").lower()
    return mime.startswith("video/") or name.endswith(
        (".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts")
    )


def _merge_safe_name(value):
    return "".join(
        char if char.isalnum() or char in "._-" else "_"
        for char in value
    )[:80] or "merged"


def merge_videos_sync(input_paths, output_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed in this runtime.")

    # The concat filter is used instead of stream-copy so videos with
    # different codecs/resolutions/time bases can still be normalized into
    # one MP4. FFmpeg documents this as the re-encoding concat approach.
    inputs = []
    for path in input_paths:
        inputs.extend(["-i", str(path)])

    filter_parts = []
    for index in range(len(input_paths)):
        filter_parts.append(
            "[{0}:v:0]scale=trunc(iw/2)*2:trunc(ih/2)*2,"
            "setsar=1,fps=30,format=yuv420p[v{0}];"
            "[{0}:a:0]aresample=async=1:first_pts=0[a{0}]".format(index)
        )

    concat_inputs = "".join(
        "[v{0}][a{0}]".format(index)
        for index in range(len(input_paths))
    )
    filter_parts.append(
        concat_inputs
        + "concat=n={}:v=1:a=1[outv][outa]".format(len(input_paths))
    )

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        *inputs,
        "-filter_complex", "".join(filter_parts),
        "-map", "[outv]",
        "-map", "[outa]",
        "-c:v", "libx264",
        "-preset", os.getenv("MERGE_PRESET", "veryfast"),
        "-crf", os.getenv("MERGE_CRF", "23"),
        "-c:a", "aac",
        "-b:a", os.getenv("MERGE_AUDIO_BITRATE", "128k"),
        "-movflags", "+faststart",
        str(output_path),
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "ffmpeg merge failed: {}".format(
                (result.stderr or "unknown ffmpeg error")[-2000:]
            )
        )
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError("ffmpeg finished without creating the merged video.")
    return output_path


async def start_merge_session(message, existing_files=None):
    files = [
        Path(path) for path in (existing_files or [])
        if Path(path).is_file()
    ]
    MERGE_SESSIONS[message.chat.id] = {
        "files": files,
        "status_message_id": None,
    }
    text = merge_status_text(MERGE_SESSIONS[message.chat.id])
    sent = await message.reply_text(text, reply_markup=merge_keyboard())
    MERGE_SESSIONS[message.chat.id]["status_message_id"] = sent.id
    return sent


async def receive_merge_video(message):
    session = MERGE_SESSIONS.get(message.chat.id)
    if not session:
        return False

    if len(session["files"]) >= MAX_MERGE_VIDEOS:
        await message.reply_text(
            "❌ Merge limit reached: <b>{}</b> videos.".format(MAX_MERGE_VIDEOS)
        )
        return True

    media = message.video or message.document
    if not message.video and not _is_video_document(message):
        return False

    file_name = (
        getattr(media, "file_name", None)
        or getattr(message.video, "file_name", None)
        or "video.mp4"
    )
    safe = _merge_safe_name(Path(file_name).stem)
    destination = (
        DOWNLOAD_DIR
        / "merge"
        / str(message.chat.id)
        / "{}_{:03d}.mp4".format(safe, len(session["files"]) + 1)
    )
    destination.parent.mkdir(parents=True, exist_ok=True)

    existing_bytes = sum(
        path.stat().st_size for path in session["files"] if path.exists()
    )
    media_size = int(getattr(media, "file_size", 0) or 0)
    if existing_bytes + media_size > MAX_MERGE_FILE_BYTES:
        await message.reply_text(
            "❌ Merge storage limit reached: <b>{}</b>.".format(
                format_size(MAX_MERGE_FILE_BYTES)
            )
        )
        return True

    status = await message.reply_text(
        "⬇️ Saving video <b>{}/{}</b> to the merge queue...".format(
            len(session["files"]) + 1, MAX_MERGE_VIDEOS
        )
    )
    try:
        downloaded = await message.download(file_name=str(destination))
        path = Path(downloaded or destination)
        if not path.is_file():
            raise RuntimeError("Telegram download completed but the file was not found.")

        session["files"].append(path)
        await safe_edit(status, merge_status_text(session))
    except Exception as exc:
        destination.unlink(missing_ok=True)
        await safe_edit(
            status,
            "❌ Could not save video: " + html.escape(str(exc)),
        )
    return True


async def run_merge_session(chat_id, status):
    session = MERGE_SESSIONS.get(chat_id)
    if not session:
        await safe_edit(status, "❌ No active merge session.")
        return

    files = [path for path in session["files"] if path.is_file()]
    if len(files) < 2:
        await safe_edit(
            status,
            "⚠️ Send at least <b>2 videos</b> before merging.",
            )
        return

    if MERGE_LOCK.locked():
        await safe_edit(status, "⏳ Another merge is already running.")
        return

    await MERGE_LOCK.acquire()
    output = (
        DOWNLOAD_DIR
        / "merge"
        / str(chat_id)
        / "merged-{}.mp4".format(int(time.time()))
    )
    try:
        await safe_edit(
            status,
            "⚙️ <b>Merging {}</b> videos with FFmpeg...\n"
            "This re-encodes the videos so different inputs can be joined.".format(
                len(files)
            ),
        )
        await asyncio.to_thread(merge_videos_sync, files, output)

        metadata = await asyncio.to_thread(video_metadata, output)
        size = output.stat().st_size
        await safe_edit(
            status,
            "☁️ Uploading merged video to GoFile...",
        )
        uploader = GofileUploader()
        started = time.monotonic()
        gofile_url = await asyncio.to_thread(uploader.upload, output)
        elapsed = max(time.monotonic() - started, 0.001)

        caption = (
            "🎞️ <b>Merged Video</b>\n"
            f"📹 Parts: <b>{len(files)}</b>\n"
            f"💾 Size: <b>{format_size(size)}</b>\n"
            f"⏱ Duration: <b>{format_duration(metadata.get('duration'))}</b>\n"
            f"⚡ GoFile upload: <b>{size / elapsed / 1048576:.2f} MB/s</b>\n\n"
            f'☁️ <a href="{html.escape(gofile_url, quote=True)}">Download merged video</a>'
        )
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬇️ Download from GoFile", url=gofile_url)]]
        )
        await app.send_message(chat_id, caption, reply_markup=markup)

        # Only clean the source files after the final merge/upload succeeds.
        for path in files:
            path.unlink(missing_ok=True)
        output.unlink(missing_ok=True)
        MERGE_SESSIONS.pop(chat_id, None)
        await safe_edit(status, "✅ <b>Merge complete.</b> The merged GoFile link was sent above.")
    except Exception as exc:
        await safe_edit(
            status,
            "❌ <b>Merge failed</b>\n" + html.escape(str(exc)),
        )
        # Keep the source videos so the user can retry without uploading them again.
        output.unlink(missing_ok=True)
    finally:
        MERGE_LOCK.release()


def episode_keyboard(ep):
    rows = []
    navigation = ep["navigation"]

    view = [InlineKeyboardButton("▶️ View Episode", url=ep["page_url"])]
    if navigation.get("series"):
        view.append(InlineKeyboardButton("📋 All Episodes", url=navigation["series"]))
    rows.append(view)

    nav = []
    if navigation.get("previous"):
        nav.append(
            InlineKeyboardButton(
                "⬅ Previous",
                callback_data="ep:" + enc(navigation["previous"]),
            )
        )
    if navigation.get("next"):
        nav.append(
            InlineKeyboardButton(
                "Next ➡",
                callback_data="ep:" + enc(navigation["next"]),
            )
        )
    if nav:
        rows.append(nav)

    quality_buttons = [
        InlineKeyboardButton(
            "⬇ " + source["label"],
            callback_data="dl:" + enc(ep["page_url"]) + ":" + enc(source["label"]),
        )
        for source in ep["sources"]
    ]
    if quality_buttons:
        rows.append(quality_buttons)

    return InlineKeyboardMarkup(rows)


def episode_text(ep):
    title = html.escape(ep["title"])
    synopsis = html.escape(ep.get("synopsis") or "")
    if len(synopsis) > 600:
        synopsis = synopsis[:597] + "..."

    total = ep.get("series_total") or "?"
    lines = [
        f"🎬 <b>{title}</b>",
        f"📺 Episode: <b>{ep.get('episode') or '?'}</b>",
        f"📚 Total Episodes: <b>{total}</b>",
    ]

    if synopsis:
        lines += ["", "📝 <b>Synopsis</b>", synopsis]

    available = ", ".join(html.escape(s["label"]) for s in ep["sources"])
    if available:
        lines += ["", f"🎥 <b>Quality:</b> {available}"]

    return "\n".join(lines)


async def show(message, ep):
    caption = episode_text(ep)
    markup = episode_keyboard(ep)

    if ep.get("thumbnail"):
        try:
            await app.send_photo(
                message.chat.id,
                ep["thumbnail"],
                caption=caption,
                reply_markup=markup,
            )
            return
        except Exception:
            pass

    await app.send_message(
        message.chat.id,
        caption,
        reply_markup=markup,
    )


def choose_source(ep, preferred=None):
    sources = ep.get("sources") or []
    if preferred:
        exact = next(
            (source for source in sources if source["label"].lower() == preferred.lower()),
            None,
        )
        if exact:
            return exact

    order = {"2160p": 4, "1440p": 3, "1080p": 2, "720p": 1}
    return max(sources, key=lambda source: order.get(source["label"].lower(), 0), default=None)


async def download_and_send(
    ep,
    target_chat,
    preferred_quality=None,
    status=None,
    post_id=None,
    series_name=None,
    series_total=None,
    keep_local=False,
    upload_to_gofile=True,
    output_path=None,
    send_telegram=True,
):
    print("[download] title={} preferred={} sources={}".format(
        ep.get("title"), preferred_quality, len(ep.get("sources") or [])
    ), flush=True)
    for item in (ep.get("sources") or []):
        print("[download] available label={} type={} url={}".format(
            item.get("label"), item.get("type"), str(item.get("url", ""))[:180]
        ), flush=True)

    source = choose_source(ep, preferred_quality)
    if not source:
        print("[download] ERROR: no source selected", flush=True)
        raise ProviderError("No downloadable video source is available")

    print("[download] selected label={} url={}".format(
        source.get("label"), str(source.get("url", ""))[:300]
    ), flush=True)

    safe = "".join(
        char if char.isalnum() or char in "._-" else "_"
        for char in ep["title"]
    )[:80]
    label = source["label"]
    output = Path(output_path) if output_path else DOWNLOAD_DIR / f"{safe}-{label}.mp4"
    thumb = DOWNLOAD_DIR / f"{safe}-{label}.jpg"
    retained_output = False

    if status:
        await status.edit_text(
            f"⬇️ Downloading <b>{html.escape(label)}</b> — "
            f"{html.escape(ep['title'])}"
        )

    try:
        await asyncio.to_thread(
            provider.download,
            source["url"],
            output,
            download_progress_factory(status) if status else None,
            ep.get("page_url"),
        )

        size = output.stat().st_size
        print("[download] completed local file size={}".format(size), flush=True)

        metadata = await asyncio.to_thread(video_metadata, output)
        print("[download] ffprobe duration={} width={} height={}".format(
            metadata.get("duration"), metadata.get("width"), metadata.get("height")
        ), flush=True)

        # Merge-series mode keeps the downloaded episode on disk. It is uploaded
        # only after all episodes have been concatenated into the final file.
        if not upload_to_gofile:
            retained_output = True
            return {"message": None, "path": output, "metadata": metadata}

        if status:
            await status.edit_text("☁️ Uploading video to GoFile...")

        # Guest upload: no API token is supplied. GoFile creates a temporary
        # guest account and returns a public downloadPage for this file.
        uploader = GofileUploader()
        started = time.monotonic()
        gofile_url = await asyncio.to_thread(
            uploader.upload,
            output,
        )
        elapsed = max(time.monotonic() - started, 0.001)
        print(
            "[gofile] uploaded {} bytes in {:.1f}s ({:.2f} MB/s) -> {}".format(
                size, elapsed, size / elapsed / 1048576, gofile_url
            ),
            flush=True,
        )

        # Telegram delivery for an individual episode:
        # thumbnail -> episode title/details -> the public GoFile download link.
        if not send_telegram:
            retained_output = bool(keep_local)
            return {
                "message": None,
                "path": output if retained_output else None,
                "gofile_url": gofile_url,
                "metadata": metadata,
                "label": label,
                "title": ep.get("title") or "Episode",
                "episode": ep.get("episode") or "?",
            }

        caption = (
            f"🎬 <b>{html.escape(ep['title'])}</b>\n"
            f"📺 <b>Episode:</b> {html.escape(str(ep.get('episode') or '?'))}\n"
            f"🎥 <b>Quality:</b> {html.escape(label)}\n"
            f"⏱ <b>Duration:</b> {format_duration(metadata['duration'])}\n\n"
            f"☁️ <b>GoFile Link:</b> "
            f'<a href="{html.escape(gofile_url, quote=True)}">Open / Download Episode</a>'
        )

        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("☁️ Open GoFile", url=gofile_url)]
        ])

        if status:
            await status.edit_text("📨 Sending GoFile link to Telegram...")

        sent = None
        if thumb_path := (await asyncio.to_thread(
            provider.download_thumbnail, ep["thumbnail"], thumb
        ) if ep.get("thumbnail") else None):
            try:
                sent = await app.send_photo(
                    chat_id=target_chat,
                    photo=str(thumb_path),
                    caption=caption,
                    reply_markup=markup,
                )
            except Exception as exc:
                print("[gofile] thumbnail message failed: {}".format(exc), flush=True)

        if sent is None:
            sent = await app.send_message(
                chat_id=target_chat,
                text=caption,
                reply_markup=markup,
            )

        print("[gofile] Telegram link message id={}".format(sent.id), flush=True)
        retained_output = bool(keep_local)
        if post_id is not None:
            mark_video_uploaded(post_id, sent.id)

        return {"message": sent, "path": output if retained_output else None}
    except Exception as exc:
        print("[download/gofile] FAILED: {}".format(exc), flush=True)
        raise
    finally:
        if not retained_output:
            output.unlink(missing_ok=True)
        thumb.unlink(missing_ok=True)


@app.on_message(filters.command("ping"))
async def ping_command(_, message):
    print("[command] /ping chat_id={}".format(message.chat.id), flush=True)
    await message.reply_text("🏓 <b>WHBot is responding.</b>")


@app.on_message(filters.command("debug"))
async def debug_command(_, message):
    print("[command] /debug chat_id={}".format(message.chat.id), flush=True)

    lines = [
        "🧪 <b>WHBot diagnostics</b>",
        "",
        "✅ Bot handler: working",
        "📁 DB: <code>{}</code>".format(DB_PATH),
    ]

    try:
        con = init_db()
        try:
            posts = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
            series = con.execute("SELECT COUNT(*) FROM series").fetchone()[0]
        finally:
            con.close()

        lines += [
            "📄 Posts: <b>{}</b>".format(posts),
            "📚 Series: <b>{}</b>".format(series),
            "📥 Download channel: <code>{}</code>".format(
                DOWNLOAD_CHANNEL_ID or "NOT SET"
            ),
            "",
        ]
    except Exception as exc:
        lines += [
            "❌ DB check failed: <code>{}</code>".format(html.escape(str(exc))),
            "",
        ]

    # Identify the exact bot account represented by BOT_TOKEN.
    try:
        me = await app.get_me()
        bot_username = "@{}".format(me.username) if me.username else "(no username)"
        lines += [
            "🤖 <b>Pyrogram bot identity</b>",
            "Name: <b>{}</b>".format(html.escape(me.first_name or "")),
            "Username: <b>{}</b>".format(html.escape(bot_username)),
            "User ID: <code>{}</code>".format(me.id),
            "",
        ]
        print(
            "[telegram debug] bot identity: id={} username={}".format(
                me.id, bot_username
            ),
            flush=True,
        )
    except Exception as exc:
        lines += [
            "❌ <b>get_me()</b> failed: <code>{}</code>".format(
                html.escape(str(exc))
            ),
            "",
        ]
        print("[telegram debug] get_me failed: {}".format(exc), flush=True)

    if not DOWNLOAD_CHANNEL_ID:
        lines.append("⚠️ DOWNLOAD_CHANNEL_ID is not configured.")
        await message.reply_text("\n".join(lines))
        return

    # Also inspect the active Pyrogram dialog cache. If the channel is present
    # here, the session already knows the peer even when direct numeric lookup fails.
    try:
        found = []
        async for dialog in app.get_dialogs():
            chat = dialog.chat
            if chat and getattr(chat, "id", None) == int(DOWNLOAD_CHANNEL_ID):
                found.append(chat)
                break
        if found:
            chat = found[0]
            lines += [
                "✅ <b>get_dialogs()</b>: channel found in Pyrogram dialogs",
                "Dialog ID: <code>{}</code>".format(chat.id),
                "Dialog title: <b>{}</b>".format(
                    html.escape(str(getattr(chat, "title", None) or getattr(chat, "username", None) or "unknown"))
                ),
            ]
            print(
                "[telegram debug] channel found in dialogs: id={} title={}".format(
                    chat.id,
                    getattr(chat, "title", None) or getattr(chat, "username", None) or "unknown",
                ),
                flush=True,
            )
        else:
            lines += [
                "⚠️ <b>get_dialogs()</b>: configured channel was NOT found",
            ]
            print(
                "[telegram debug] channel NOT found in dialogs: {}".format(
                    DOWNLOAD_CHANNEL_ID
                ),
                flush=True,
            )
    except Exception as exc:
        lines += [
            "❌ <b>get_dialogs()</b>: FAILED",
            "<code>{}</code>".format(html.escape(str(exc))),
        ]
        print("[telegram debug] get_dialogs FAILED: {}".format(exc), flush=True)

    # Test the two relevant Pyrogram peer-resolution paths separately.
    try:
        chat = await app.get_chat(DOWNLOAD_CHANNEL_ID)
        title = (
            getattr(chat, "title", None)
            or getattr(chat, "username", None)
            or "unknown"
        )
        chat_type = (
            getattr(getattr(chat, "type", None), "value", None)
            or getattr(chat, "type", None)
            or "unknown"
        )
        lines += [
            "✅ <b>get_chat()</b>: OK",
            "Channel ID: <code>{}</code>".format(chat.id),
            "Title: <b>{}</b>".format(html.escape(str(title))),
            "Type: <b>{}</b>".format(html.escape(str(chat_type))),
        ]
        print(
            "[telegram debug] get_chat OK: id={} title={} type={}".format(
                chat.id, title, chat_type
            ),
            flush=True,
        )
    except Exception as exc:
        lines += [
            "❌ <b>get_chat()</b>: FAILED",
            "<code>{}</code>".format(html.escape(str(exc))),
        ]
        print("[telegram debug] get_chat FAILED: {}".format(exc), flush=True)

    try:
        peer = await app.resolve_peer(DOWNLOAD_CHANNEL_ID)
        lines += [
            "✅ <b>resolve_peer()</b>: OK",
            "Peer type: <code>{}</code>".format(type(peer).__name__),
        ]
        print(
            "[telegram debug] resolve_peer OK: {}".format(type(peer).__name__),
            flush=True,
        )
    except Exception as exc:
        lines += [
            "❌ <b>resolve_peer()</b>: FAILED",
            "<code>{}</code>".format(html.escape(str(exc))),
        ]
        print(
            "[telegram debug] resolve_peer FAILED: {}".format(exc),
            flush=True,
        )

    await message.reply_text("\n".join(lines))



@app.on_message(filters.all, group=99)
async def incoming_debug(_, message):
    # Log only the message type/command, never tokens or full message text.
    try:
        text_value = getattr(message, "text", None) or getattr(message, "caption", None) or ""
        if text_value.startswith("/"):
            command_name = text_value.split()[0].split("@")[0]
            print("[incoming] command={} chat_id={}".format(
                command_name, message.chat.id
            ), flush=True)
    except Exception as exc:
        print("[incoming] log error: {}".format(exc), flush=True)


@app.on_message(filters.command("help", prefixes=["/"]))
async def help_command(_, message):
    print("[command] /help chat_id={}".format(message.chat.id), flush=True)
    await message.reply_text(
        "<b>WHBot Help</b>\n\n"
        "🔎 <b>Browse</b>\n"
        "/latest — show one latest post\n"
        "/search &lt;query&gt; — search the local catalog\n"
        "/episode &lt;URL&gt; — open a specific episode\n\n"
        "⚙️ <b>Catalog</b>\n"
        "/stats — catalog statistics\n"
        "/crawl — crawl series pages into the catalog\n"
        "/download — download an entire series to the download channel\n"
        "/publish — publish catalog metadata\n"
        "/setchannel — configure the download channel\n\n"
        "🤖 <b>Auto uploader</b>\n"
        "/auto — download and upload all pending episodes sequentially\n"
        "/auto 720p — use 720p when available\n"
        "/auto stop — stop after the current transfer\n\n"
        "⬇️ Open an episode to view qualities and download it."
    )


@app.on_message(filters.regex(r"^/setchannel(?:@\\w+)?(?:\\s+.*)?$", flags=re.IGNORECASE), group=-1)
async def setchannel_command(_, message):
    print(
        "[telegram setup] /setchannel received chat_id={} user_id={} chat_type={}".format(
            getattr(getattr(message, "chat", None), "id", None),
            getattr(getattr(message, "from_user", None), "id", None),
            getattr(getattr(getattr(message, "chat", None), "type", None), "value", None),
        ),
        flush=True,
    )
    if not owner_only(message):
        await message.reply_text("❌ This command is restricted to the bot owner.")
        return
    chat_type = getattr(getattr(message.chat, "type", None), "value", None)
    if chat_type != "private":
        await message.reply_text("⚠️ Please use /setchannel in the bot's private chat.")
        return
    if not message.from_user:
        await message.reply_text("❌ Telegram did not provide your user identity. Please send /setchannel again in the bot's private chat.")
        return

    CHANNEL_SETUP_USERS.add(message.from_user.id)

    # If the owner forwards any message from the target channel here, we can
    # learn the real channel peer directly from Telegram instead of relying on
    # a possibly stale DOWNLOAD_CHANNEL_ID/peer cache.
    forwarded_chat = getattr(message, "forward_from_chat", None)
    if forwarded_chat and getattr(forwarded_chat, "type", None) and getattr(getattr(forwarded_chat, "type", None), "value", None) == "channel":
        channel_id = getattr(forwarded_chat, "id", None)
        if channel_id:
            try:
                me = await app.get_me()
                member = await app.get_chat_member(channel_id, me.id)
                status_value = getattr(getattr(member, "status", None), "value", None) or str(getattr(member, "status", ""))
                if status_value in {"administrator", "owner"}:
                    save_download_channel_setting(channel_id)
                    CHANNEL_SETUP_USERS.clear()
                    title = getattr(forwarded_chat, "title", None) or getattr(forwarded_chat, "username", None) or "Unknown"
                    await message.reply_text(
                        "✅ <b>Download channel verified!</b>\n\n"
                        "📥 Channel: <b>{}</b>\n"
                        "🆔 ID: <code>{}</code>\n"
                        "👑 Bot status: <b>{}</b>".format(
                            html.escape(str(title)), channel_id, html.escape(str(status_value))
                        )
                    )
                    print("[telegram setup] VERIFIED forwarded channel id={} title={} status={}".format(channel_id, title, status_value), flush=True)
                    return
                await message.reply_text("❌ I found the forwarded channel, but I am not an administrator there.")
                return
            except Exception as exc:
                print("[telegram setup] forwarded channel verification failed: {}".format(exc), flush=True)
                await message.reply_text(
                    "❌ I found the forwarded channel, but Telegram could not verify my access yet. "
                    "Make sure I am an Administrator in that channel, then send /setchannel again with a forwarded message.\n\n"
                    "<code>{}</code>".format(html.escape(str(exc)))
                )
                return

    await message.reply_text(
        "📥 <b>Download channel setup</b>\n\n"
        "1️⃣ Add me to <b>Hentaiiiiii</b> as an <b>Administrator</b>.\n"
        "2️⃣ Send me <b>any forwarded message from Hentaiiiiii</b>.\n"
        "3️⃣ I will detect the channel ID, verify my admin access, and save it automatically.\n\n"
        "👤 Owner ID: <code>7367490186</code>\n"
        "📌 Current channel ID: <code>-1003671348585</code>"
    )


@app.on_message(filters.regex(r"^/verify(?:@\w+)?(?:\s+.*)?$", flags=re.IGNORECASE), group=-1)
async def verify_channel_command(_, message):
    chat = getattr(message, "chat", None)
    chat_type = getattr(getattr(chat, "type", None), "value", None)
    if not chat or chat_type != "channel":
        return
    if not BOT_OWNER_ID:
        print("[telegram setup] BOT_OWNER_ID is not configured; /verify rejected", flush=True)
        return
    if message.from_user and message.from_user.id != BOT_OWNER_ID:
        return
    if not message.from_user and not CHANNEL_SETUP_USERS:
        return
    try:
        me = await app.get_me()
        member = await app.get_chat_member(chat.id, me.id)
        status = getattr(member, "status", None)
        status_value = getattr(status, "value", None) or str(status)
        if status_value not in {"administrator", "owner"}:
            await app.send_message(chat.id, "❌ I received /verify, but I am not an administrator of this channel. Please promote me to administrator and send /verify again.")
            return
        peer = await app.resolve_peer(chat.id)
        save_download_channel_setting(chat.id)
        CHANNEL_SETUP_USERS.clear()
        title = getattr(chat, "title", None) or getattr(chat, "username", None) or "Unknown"
        print("[telegram setup] VERIFIED channel id={} title={} status={} peer={}".format(chat.id, title, status_value, type(peer).__name__), flush=True)
        await app.send_message(
            chat.id,
            "✅ <b>Download channel verified!</b>\n\n"
            "📥 Channel: <b>{}</b>\n"
            "🆔 ID: <code>{}</code>\n"
            "👑 Bot status: <b>{}</b>\n\n"
            "This channel is now saved as the WatchHentaiBot download channel.".format(html.escape(str(title)), chat.id, html.escape(status_value)),
        )
    except Exception as exc:
        print("[telegram setup] /verify FAILED: {}".format(exc), flush=True)
        try:
            await app.send_message(chat.id, "❌ <b>Channel verification failed</b>\n<code>{}</code>".format(html.escape(str(exc))))
        except Exception:
            pass



@app.on_message(filters.command("merge"))
async def merge_command(_, message):
    print("[command] /merge chat_id={}".format(message.chat.id), flush=True)
    existing = MERGE_SESSIONS.get(message.chat.id, {}).get("files", [])
    await start_merge_session(message, existing_files=existing)


@app.on_message((filters.video | filters.document))
async def merge_media(_, message):
    # A video sent while a merge session is active is added to that session.
    # Documents are accepted only when their MIME type or extension identifies
    # them as a video.
    if message.chat.id not in MERGE_SESSIONS:
        return
    await receive_merge_video(message)


@app.on_message(filters.command("start"))
async def start(_, message):
    await message.reply_text(
        "WHBot ready.\n\n"
        "/latest - latest single catalog post\n"
        "/search <query> - search local catalog\n"
        "/episode <URL> - open an episode\n"
        "/stats - catalog statistics\n"
        "/crawl - crawl series catalog\n"
        "/download - download a complete series to the download channel\n"
        "/merge - collect multiple videos and merge them into one\n"
        "/publish - publish catalog metadata\n"
        "/auto - sequentially upload pending episodes\n"
        "/setchannel - configure the download channel"
    )


@app.on_message(filters.command("latest"))
async def latest(_, message):
    print("[command] /latest chat_id={}".format(message.chat.id), flush=True)
    status = await message.reply_text("🔎 Loading latest post...")
    try:
        rows = await asyncio.to_thread(
            db_rows,
            "SELECT url FROM posts ORDER BY id DESC LIMIT 1",
        )
        if not rows:
            await status.edit_text("Catalog is empty. Run /crawl first.")
            return

        ep = await asyncio.to_thread(provider.get_episode, rows[0][0], True)
        await status.delete()
        await show(message, ep)
    except Exception as exc:
        await status.edit_text("❌ " + str(exc))


@app.on_message(filters.command("search"))
async def search(_, message):
    print("[command] /search chat_id={}".format(message.chat.id), flush=True)
    if len(message.command) < 2:
        await message.reply_text("Usage: /search <title>")
        return

    query = " ".join(message.command[1:]).strip()
    status = await message.reply_text(
        f"🔎 Searching local catalog for <b>{html.escape(query)}</b>..."
    )
    try:
        like = "%" + query.replace("%", "\\%").replace("_", "\\_") + "%"
        rows = await asyncio.to_thread(
            db_rows,
            "SELECT url FROM posts WHERE title LIKE ? ESCAPE '\\' "
            "OR synopsis LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT 20",
            (like, like),
        )
        if not rows:
            await status.edit_text("No matching posts in local catalog.")
            return

        await status.delete()
        # Search intentionally shows results one at a time, each with navigation.
        for (url,) in rows[:10]:
            try:
                ep = await asyncio.to_thread(provider.get_episode, url, True)
                await show(message, ep)
            except Exception as exc:
                await message.reply_text("⚠️ Could not load result: " + str(exc))
    except Exception as exc:
        await status.edit_text("❌ " + str(exc))


@app.on_message(filters.command("stats"))
async def stats(_, message):
    try:
        row = await asyncio.to_thread(
            db_rows,
            "SELECT COUNT(*), "
            "SUM(CASE WHEN published=1 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN video_uploaded=1 THEN 1 ELSE 0 END) FROM posts",
        )
        total, published, uploaded = row[0]
        await message.reply_text(
            f"📊 <b>Catalog</b>\n\n"
            f"Posts: <b>{total or 0}</b>\n"
            f"Metadata published: <b>{published or 0}</b>\n"
            f"Videos uploaded: <b>{uploaded or 0}</b>"
        )
    except Exception as exc:
        await message.reply_text("❌ " + str(exc))


def series_crawl_progress_factory(status):
    state = {"last": 0.0}

    def progress(page, total_pages, processed, new_count, series):
        now = time.monotonic()
        if now - state["last"] < 5.0:
            return
        state["last"] = now
        name = series.get("name") if series else None
        episodes = series.get("total_episodes") if series else None
        current = (
            f"🎬 Current: <b>{html.escape(name)}</b>\n"
            f"📺 Episodes: <b>{episodes or 0}</b>\n"
            if name else ""
        )
        text = (
            "🕷️ <b>Series crawler running</b>\n\n"
            f"📄 Page: <b>{page}/{total_pages}</b>\n"
            f"📚 Series processed: <b>{processed}</b>\n"
            f"🆕 New series: <b>{new_count}</b>\n"
            + current + "\n"
            "🔄 Updating every 5 seconds"
        )
        asyncio.run_coroutine_threadsafe(safe_edit(status, text), app.loop)

    return progress


@app.on_message(filters.command("crawl"))
async def crawl_command(_, message):
    CRAWL_STATES[message.chat.id] = {"stage": "start"}
    await message.reply_text(
        "🕷 <b>Catalog crawler</b>\n\n"
        "Send the <b>starting series page number</b>.\n"
        "<b>1</b> = the first series page.\n"
        "<b>2</b> = the second series page.\n"
        "Example: <code>1</code>"
    )


@app.on_message(filters.text)
async def crawl_input(_, message):
    if message.text.startswith("/"):
        return

    if DOWNLOAD_STATES.get(message.chat.id):
        await start_series_download(message, message.text.strip())
        return

    state = CRAWL_STATES.get(message.chat.id)
    if not state:
        return

    try:
        value = int(message.text.strip())
        if value < 1:
            raise ValueError
    except ValueError:
        await message.reply_text(
            "❌ Send a whole number starting from <b>1</b>, such as <b>1</b>, <b>7</b> or <b>121</b>."
        )
        return

    if state["stage"] == "start":
        state["start"] = value
        state["stage"] = "end"
        await message.reply_text(
            "✅ Starting page: <b>{}</b>\n\n"
            "Now send the <b>ending page number</b>.\n"
            "It must be greater than or equal to the starting page.".format(value)
        )
        return

    start_page = state["start"]
    end_page = value

    if end_page < start_page:
        await message.reply_text("❌ Ending page cannot be smaller than starting page.")
        return

    CRAWL_STATES.pop(message.chat.id, None)

    status = await message.reply_text(
        "🕷 <b>Crawling series pages {} → {}</b>\n"
        "1 = /series/; 2+ = /series/page/N/\n\n"
        "Each series is saved with its thumbnail, title, episode count, series URL and all episode URLs.\n"
        "New series are then published to the configured Telegram channel.".format(
            start_page, end_page
        )
    )

    try:
        with BACKGROUND_LOCK:
            progress = series_crawl_progress_factory(status)
            result = await asyncio.to_thread(
                crawl_series, start_page, end_page, 0.25, None, progress
            )

            new_series_ids = result.get("new_ids", [])
            published = 0
            if new_series_ids:
                published = await asyncio.to_thread(
                    publish_series_ids, new_series_ids
                )

        await status.edit_text(
            "✅ <b>Series crawl completed</b>\n\n"
            "Pages: <b>{} → {}</b>\n"
            "Series processed: <b>{}</b>\n"
            "New series saved: <b>{}</b>\n"
            "Published to channel: <b>{}</b>\n\n"
            "Saved data: title, thumbnail, episode count, series URL and all episode URLs.".format(
                start_page,
                end_page,
                result.get("processed", 0),
                len(result.get("new_ids", [])),
                published,
            )
        )
    except Exception as exc:
        await status.edit_text("❌ Series crawl failed: " + html.escape(str(exc)))


@app.on_message(filters.regex(r"^/download(?:@\w+)?(?:\s+.*)?$", flags=re.I), group=-1)
async def download_command(_, message):
    print("[command] /download chat_id={}".format(message.chat.id), flush=True)
    if not DOWNLOAD_CHANNEL_ID:
        await message.reply_text("❌ DOWNLOAD_CHANNEL_ID is not configured.")
        return

    raw_text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    parts = raw_text.strip().split(None, 1)
    if len(parts) == 2 and parts[1].strip():
        await start_series_download(message, parts[1].strip())
        return

    DOWNLOAD_STATES[message.chat.id] = True
    await message.reply_text(
        "📥 <b>Series downloader</b>\n\n"
        "Send the series URL.\n"
        "Example:\n"
        "<code>https://watchhentai.net/series/kuro-gal-a-la-carte-id-01/</code>"
    )


async def resolve_series(series_url):
    if not re.match(r"^https?://watchhentai\.net/series/[^\s]+/?$", series_url, re.I):
        raise ProviderError("Please send a valid WatchHentai series URL.")

    series = await asyncio.to_thread(provider.get_series, series_url)
    episode_items = await asyncio.to_thread(
        provider.series_episodes, series["series_url"]
    )
    if not episode_items:
        raise ProviderError("No episodes found in this series.")

    series["total_episodes"] = series.get("total_episodes") or len(episode_items)
    return series, episode_items


async def show_series_preview(message, series, episode_items):
    total = series.get("total_episodes") or len(episode_items)
    caption = (
        f"🎬 <b>{html.escape(series['name'])}</b>\n"
        f"📺 Episodes found: <b>{len(episode_items)}</b>\n"
        f"📚 Total Episodes: <b>{total}</b>\n\n"
        "🔗 Episode pages have been collected.\n"
        "⬇️ Download episodes individually, or download every episode first and merge them into one video."
    )
    markup = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "⬇️ Download All Episodes",
                callback_data="sd:" + enc(series["series_url"]),
            )
        ], [
            InlineKeyboardButton(
                "🎞️ Download & Merge All",
                callback_data="sm:" + enc(series["series_url"]),
            )
        ]]
    )

    if series.get("thumbnail"):
        try:
            await app.send_photo(
                message.chat.id,
                series["thumbnail"],
                caption=caption,
                reply_markup=markup,
            )
            return
        except Exception as exc:
            print("[series preview] thumbnail failed: {}".format(exc), flush=True)

    await app.send_message(
        message.chat.id,
        caption,
        reply_markup=markup,
    )


def series_episode_keyboard(episodes):
    """Build one GoFile URL button per episode in series order."""
    rows = []
    for item in episodes:
        number = str(item.get("episode") or item.get("index") or "?")
        rows.append([
            InlineKeyboardButton("▶️ Episode {}".format(number), url=item["gofile_url"])
        ])
    return InlineKeyboardMarkup(rows) if rows else None


async def send_series_episode_menu(chat_id, series, episode_results):
    ordered = sorted(
        episode_results,
        key=lambda item: (
            int(item["episode"]) if str(item.get("episode", "")).isdigit() else 10**9,
            int(item.get("index", 0)),
        ),
    )
    text = (
        f"🎬 <b>{html.escape(series['name'])}</b>\n"
        f"📺 <b>{len(ordered)} Episodes</b>\n"
        "━━━━━━━━━━━━━━"
    )
    markup = series_episode_keyboard(ordered)
    if markup:
        await app.send_message(chat_id, text, reply_markup=markup)
    else:
        await app.send_message(chat_id, text + "\n\n❌ No episode links were generated.")


async def download_series(
    message,
    series_url,
    status=None,
    raise_on_error=False,
    acquire_lock=True,
    fallback_chat_id=None,
    merge_chat_id=None,
    merge_series=False,
):

    upload_chat = DOWNLOAD_CHANNEL_ID
    channel_ok = await validate_download_channel()
    if not channel_ok:
        if fallback_chat_id is not None:
            upload_chat = fallback_chat_id
            print("[telegram] using user-chat fallback: {}".format(upload_chat), flush=True)
            if status is not None:
                await safe_edit(
                    status,
                    "⚠️ Download channel unavailable. Videos will be sent to your chat instead.",
                )
        else:
            error = ProviderError(
                "DOWNLOAD_CHANNEL_ID is invalid or inaccessible. Add the bot to the target channel as an administrator and set the channel's numeric ID (usually -100...) or @username in DOWNLOAD_CHANNEL_ID."
            )
            if status is not None:
                await safe_edit(status, "❌ " + html.escape(str(error)))
            if raise_on_error:
                raise error
            return
    if message is not None:
        DOWNLOAD_STATES.pop(message.chat.id, None)

    if acquire_lock and AUTO_LOCK.locked():
        error = ProviderError("Another download/upload job is already running.")
        if message is not None:
            await message.reply_text("⏳ Another download/upload job is already running.")
        if raise_on_error:
            raise error
        return

    owns_lock = False
    if acquire_lock:
        await AUTO_LOCK.acquire()
        owns_lock = True

    if status is None:
        if message is None:
            raise ProviderError("A status message is required for background downloads.")
        status = await message.reply_text("🔎 Resolving series and episode list...")

    try:
        series, episode_items = await resolve_series(series_url)
        total = series.get("total_episodes") or len(episode_items)

        await safe_edit(
            status,
            "📚 <b>{}</b>\nEpisodes found: <b>{}</b>\n"
            "🔗 Resolving video sources only when each episode is downloaded...".format(
                html.escape(series["name"]), len(episode_items)
            ),
        )

        def episode_number(item):
            m = re.search(r"episode[-\s]+(\d+)", item["page_url"], re.I)
            return int(m.group(1)) if m else 10**9

        episode_items.sort(key=episode_number)
        series_episode_results = []

        for index, item in enumerate(episode_items, 1):
            print(
                "[series download] episode {}/{} url={}".format(
                    index, total, item["page_url"]
                ),
                flush=True,
            )

            # The series preview deliberately does not resolve media sources.
            # Resolve the episode page and its actual stream/download sources here,
            # immediately before that episode is downloaded.
            ep = await asyncio.to_thread(
                provider.get_episode, item["page_url"], True
            )
            sources = ep.get("sources") or []
            if not sources:
                raise ProviderError(
                    f"Episode {index}/{total} has no downloadable video source."
                )

            order = {"2160p": 4, "1440p": 3, "1080p": 2, "720p": 1}
            sources = sorted(
                sources,
                key=lambda source: order.get(
                    str(source.get("label", "")).lower(), 0
                ),
                reverse=True,
            )

            last_error = None
            completed = False
            for source in sources:
                label = str(source.get("label") or "Unknown")
                await safe_edit(
                    status,
                    "⬇️ <b>Episode {}/{}</b> — resolving/downloading <b>{}</b>...".format(
                        index, total, html.escape(label)
                    ),
                )
                print(
                    "[series download] trying episode {} quality={} merge_mode={}".format(
                        index, label, merge_series
                    ),
                    flush=True,
                )
                try:
                    merge_output = None
                    if merge_series:
                        safe_episode = "".join(
                            char if char.isalnum() or char in "._-" else "_"
                            for char in ep["title"]
                        )[:70] or "episode"
                        merge_output = (
                            DOWNLOAD_DIR
                            / "merge"
                            / str(fallback_chat_id or (message.chat.id if message else 0))
                            / "{}_{:03d}-{}-{}.mp4".format(
                                safe_episode, index, label, int(time.time())
                            )
                        )
                        merge_output.parent.mkdir(parents=True, exist_ok=True)

                    result = await download_and_send(
                        ep,
                        upload_chat,
                        preferred_quality=label,
                        status=status,
                        series_name=series["name"],
                        series_total=total,
                        keep_local=bool(merge_chat_id or merge_series),
                        upload_to_gofile=not merge_series,
                        output_path=merge_output,
                        send_telegram=False,
                    )
                    if merge_series and result.get("path"):
                        session = MERGE_SESSIONS.setdefault(
                            fallback_chat_id or (message.chat.id if message else 0),
                            {"files": [], "status_message_id": None},
                        )
                        session["files"].append(Path(result["path"]))
                    elif not merge_series and result.get("gofile_url"):
                        series_episode_results.append({
                            "index": index,
                            "episode": result.get("episode") or ep.get("episode") or index,
                            "title": result.get("title") or ep.get("title") or f"Episode {index}",
                            "gofile_url": result["gofile_url"],
                        })
                    completed = True
                    break
                except Exception as source_exc:
                    last_error = source_exc
                    print(
                        "[series download] quality {} failed: {}".format(
                            label, source_exc
                        ),
                        flush=True,
                    )

            if not completed:
                raise ProviderError(
                    "Episode {}/{} failed for all available sources: {}".format(
                        index, total, last_error or "unknown error"
                    )
                )

            await safe_edit(
                status,
                (
                    "✅ <b>Episode {}/{}</b> downloaded and kept for merging."
                    if merge_series
                    else "✅ <b>Episode {}/{}</b> uploaded to download channel."
                ).format(index, total),
            )

        if not merge_series:
            await safe_edit(
                status,
                "🎉 <b>All {} episodes uploaded to GoFile.</b>\n"
                "📋 Sending the episode menu...".format(len(series_episode_results)),
            )
            await send_series_episode_menu(upload_chat, series, series_episode_results)
        else:
            await safe_edit(
                status,
                "🎉 <b>Series download complete</b>\n\n"
                f"{html.escape(series['name'])}\n"
                f"Episodes: <b>{total}</b>",
            )

        if merge_series:
            chat_id = fallback_chat_id or (message.chat.id if message else None)
            session = MERGE_SESSIONS.get(chat_id)
            if not session or len(session.get("files") or []) != total:
                raise ProviderError(
                    "Not all episodes were downloaded, so the complete series cannot be merged."
                )
            session["files"] = [
                Path(path) for path in session["files"]
                if Path(path).is_file()
            ]
            if len(session["files"]) != total:
                raise ProviderError(
                    "One or more downloaded episode files are missing from disk."
                )
            await safe_edit(
                status,
                "🎞️ <b>All {} episodes downloaded.</b>\n"
                "🔗 Starting FFmpeg merge...".format(total),
            )
            await run_merge_session(chat_id, status)
        elif merge_chat_id:
            session = MERGE_SESSIONS.get(merge_chat_id)
            if session and session.get("files"):
                session["files"] = [
                    Path(path) for path in session["files"]
                    if Path(path).is_file()
                ]
                merge_message = await app.send_message(
                    merge_chat_id,
                    merge_status_text(session),
                    reply_markup=merge_keyboard(),
                )
                session["status_message_id"] = merge_message.id
    except Exception as exc:
        print(
            "[series download] FAILED: {}: {}".format(type(exc).__name__, exc),
            flush=True,
        )
        await safe_edit(
            status,
            "❌ Download failed: " + html.escape(str(exc)),
        )
        if raise_on_error:
            raise
    finally:
        if owns_lock:
            AUTO_LOCK.release()


async def start_series_download(message, series_url):
    # /download is a two-step workflow:
    # 1) scrape the series page and show a preview;
    # 2) resolve actual media sources only after the user presses Download All.
    if message is None:
        raise ProviderError("A message is required for /download preview.")

    DOWNLOAD_STATES.pop(message.chat.id, None)
    status = await message.reply_text("🔎 Scraping series page and collecting episodes...")
    try:
        series, episode_items = await resolve_series(series_url)
        await status.delete()
        await show_series_preview(message, series, episode_items)
    except Exception as exc:
        await safe_edit(status, "❌ Series lookup failed: " + html.escape(str(exc)))




@app.on_message(filters.command("publish"))
async def publish_command(_, message):
    status = await message.reply_text("📢 Publishing pending catalog posts...")
    try:
        with BACKGROUND_LOCK:
            count = await asyncio.to_thread(publish_pending, 20)
        await status.edit_text(
            "✅ Published <b>{}</b> pending post(s).".format(count)
        )
    except Exception as exc:
        await status.edit_text("❌ Publish failed: " + str(exc))


@app.on_message(filters.command("episode"))
async def episode(_, message):
    print("[command] /episode chat_id={}".format(message.chat.id), flush=True)
    if len(message.command) < 2:
        await message.reply_text("Usage: /episode <URL>")
        return

    try:
        ep = await asyncio.to_thread(
            provider.get_episode, " ".join(message.command[1:]), True
        )
        await show(message, ep)
    except Exception as exc:
        await message.reply_text("❌ " + str(exc))


PROGRESS_BAR_LENGTH = 12
PROGRESS_UPDATE_INTERVAL = max(float(os.getenv("DOWNLOAD_PROGRESS_INTERVAL", "5.0")), 2.0)


def format_time(seconds):
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def format_size(value):
    if not value:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def progress_bar(percent):
    percent = max(0.0, min(float(percent), 100.0))
    filled = int(round(PROGRESS_BAR_LENGTH * percent / 100))
    return "█" * filled + "░" * (PROGRESS_BAR_LENGTH - filled)


def progress_text(prefix, current, total, started):
    elapsed = max(time.monotonic() - started, 0.001)
    speed = current / elapsed
    percent = current * 100 / total if total else 0.0
    eta = (total - current) / speed if total and speed > 0 else 0

    bar = progress_bar(percent)
    current_size = format_size(current)
    total_size = format_size(total) if total else "?"
    speed_size = format_size(speed) + "/s"

    return (
        f"{prefix}\n"
        f"<code>[{bar}] {percent:5.1f}%</code>\n"
        f"📦 <b>{current_size}</b> / <b>{total_size}</b>\n"
        f"⚡ <b>{speed_size}</b>\n"
        f"⏱ <b>{format_time(elapsed)}</b> elapsed  •  "
        f"🕐 <b>{format_time(eta)}</b> left"
    )


def crawl_progress_factory(status):
    state = {"last": 0.0}

    def progress(page, total_pages, found, imported, phase):
        now = time.monotonic()
        if phase != "completed" and now - state["last"] < 2.0:
            return
        state["last"] = now

        percent = page * 100 / total_pages if total_pages else 0
        bar = progress_bar(percent)

        if phase == "completed":
            text = (
                "🕷️ <b>Crawl completed</b>\n"
                f"<code>[{bar}] 100.0%</code>\n"
                f"📄 Pages: <b>{total_pages}/{total_pages}</b>\n"
                f"🆕 New posts: <b>{imported}</b>"
            )
        else:
            text = (
                "🕷️ <b>Crawling catalog</b>\n"
                f"<code>[{bar}] {percent:5.1f}%</code>\n"
                f"📄 Page: <b>{page}/{total_pages}</b>\n"
                f"🔎 Found: <b>{found}</b> episodes\n"
                f"🆕 Imported: <b>{imported}</b>"
            )

        asyncio.run_coroutine_threadsafe(
            safe_edit(status, text),
            app.loop,
        )

    return progress


async def safe_edit(status, text):
    if status is None:
        return
    try:
        await status.edit_text(text)
    except RPCError:
        pass


async def upload_progress(current, total, status, started):
    if status is None:
        return
    now = time.monotonic()
    last = getattr(upload_progress, "_last", 0.0)
    if now - last < TELEGRAM_PROGRESS_INTERVAL and current < total:
        return
    upload_progress._last = now
    await safe_edit(
        status,
        progress_text("📤 <b>Uploading to Telegram</b>", current, total, started),
    )


def download_progress_factory(status):
    state = {"last": 0.0}

    def progress(current, total, started):
        if status is None:
            return
        now = time.monotonic()
        if now - state["last"] < PROGRESS_UPDATE_INTERVAL and (not total or current < total):
            return
        state["last"] = now
        asyncio.run_coroutine_threadsafe(
            safe_edit(
                status,
                progress_text("⬇️ Downloading...", current, total, started),
            ),
            app.loop,
        )

    return progress


async def run_auto(status):
    if not DOWNLOAD_CHANNEL_ID:
        raise RuntimeError("DOWNLOAD_CHANNEL_ID is required for /auto")

    con = init_db()
    try:
        rows = con.execute(
            """SELECT id,url,name,total_episodes
               FROM series
               ORDER BY id ASC"""
        ).fetchall()
    finally:
        con.close()

    if not rows:
        await status.edit_text(
            "✅ Series auto queue is empty. Run /crawl first."
        )
        return

    AUTO_STOP.clear()
    total = len(rows)
    completed = 0

    await AUTO_LOCK.acquire()
    try:
        for index, row in enumerate(rows, 1):
            if AUTO_STOP.is_set():
                await status.edit_text(
                    f"⏹ <b>Series auto downloader stopped.</b>\n"
                    f"Completed: <b>{completed}/{total}</b>"
                )
                return

            series_id, series_url, series_name, episode_total = row

            await safe_edit(
                status,
                f"🤖 <b>Auto series downloader</b>\n"
                f"Series: <b>{html.escape(series_name or series_url)}</b>\n"
                f"Series queue: <b>{index}/{total}</b>\n"
                f"Episodes: <b>{episode_total or '?'}</b>"
            )

            try:
                await download_series(
                    message=None,
                    series_url=series_url,
                    status=status,
                    raise_on_error=True,
                    acquire_lock=False,
                )
                completed += 1

                await safe_edit(
                    status,
                    f"✅ <b>Series {completed}/{total} completed</b>\n"
                    f"{html.escape(series_name or series_url)}"
                )
            except Exception as exc:
                await safe_edit(
                    status,
                    f"⚠️ <b>Series failed</b>\n"
                    f"{html.escape(series_name or series_url)}\n"
                    f"{html.escape(str(exc))}\n\n"
                    f"Continuing: <b>{completed}/{total}</b>"
                )
                await asyncio.sleep(1)

        await safe_edit(
            status,
            f"🎉 <b>Series auto downloader finished</b>\n"
            f"Completed: <b>{completed}/{total}</b>"
        )
    finally:
        AUTO_LOCK.release()


@app.on_message(filters.command("auto"))
async def auto_command(_, message):
    print("[command] /auto chat_id={}".format(message.chat.id), flush=True)
    args = message.command[1:]
    if args and args[0].lower() == "stop":
        AUTO_STOP.set()
        await message.reply_text(
            "⏹ Series auto downloader will stop after the current series."
        )
        return

    if args:
        await message.reply_text(
            "Usage: /auto or /auto stop\n"
            "Quality is selected automatically at the highest available level."
        )
        return

    if AUTO_LOCK.locked():
        await message.reply_text("⚠️ A download/upload job is already running.")
        return

    status = await message.reply_text("🤖 Starting series auto downloader...")

    try:
        await run_auto(status)
    except Exception as exc:
        await status.edit_text(
            "❌ Series auto downloader failed: " + html.escape(str(exc))
        )


@app.on_callback_query()
async def callback(_, query):
    print("[callback] data={}".format(query.data[:80]), flush=True)
    await query.answer()

    try:
        if query.data == "merge:add":
            existing = MERGE_SESSIONS.get(query.message.chat.id, {}).get("files", [])
            await start_merge_session(query.message, existing_files=existing)
            return

        if query.data == "merge:cancel":
            session = MERGE_SESSIONS.pop(query.message.chat.id, None)
            if session:
                for path in session.get("files", []):
                    Path(path).unlink(missing_ok=True)
            await query.message.edit_text("❌ <b>Merge session cancelled.</b>")
            return

        if query.data == "merge:run":
            status = query.message
            # Button callback is attached to the queue message itself.
            await run_merge_session(query.message.chat.id, status)
            return

        if query.data.startswith("sd:"):
            series_url = dec(query.data[3:])
            if AUTO_LOCK.locked():
                await query.message.reply_text(
                    "⏳ Another download/upload job is already running."
                )
                return

            status = await query.message.reply_text(
                "🔎 <b>Starting series download...</b>\n"
                "Each episode will be resolved individually."
            )
            try:
                await download_series(
                    message=None,
                    series_url=series_url,
                    status=status,
                    raise_on_error=False,
                    acquire_lock=True,
                    fallback_chat_id=query.message.chat.id,
                )
            except Exception as exc:
                await safe_edit(
                    status,
                    "❌ Download failed: " + html.escape(str(exc)),
                )
            return

        if query.data.startswith("sm:"):
            series_url = dec(query.data[3:])
            if AUTO_LOCK.locked():
                await query.message.reply_text(
                    "⏳ Another download/upload job is already running."
                )
                return

            chat_id = query.message.chat.id
            MERGE_SESSIONS.pop(chat_id, None)
            status = await query.message.reply_text(
                "🎞️ <b>Starting Download & Merge</b>\n"
                "Episodes will be downloaded to disk first. GoFile upload happens only after the merge."
            )
            try:
                await download_series(
                    message=None,
                    series_url=series_url,
                    status=status,
                    raise_on_error=False,
                    acquire_lock=True,
                    fallback_chat_id=chat_id,
                    merge_chat_id=chat_id,
                    merge_series=True,
                )
            except Exception as exc:
                await safe_edit(
                    status,
                    "❌ Download & merge failed: " + html.escape(str(exc)),
                )
            return

        if query.data.startswith("ep:"):
            ep = await asyncio.to_thread(
                provider.get_episode, dec(query.data[3:]), True
            )
            await show(query.message, ep)
            return

        if not query.data.startswith("dl:"):
            return

        encoded_page, encoded_label = query.data[3:].split(":", 1)
        page_url = dec(encoded_page)
        label = dec(encoded_label)
        status = await query.message.reply_text(
            f"⬇️ <b>Preparing {html.escape(label)}</b>..."
        )

        try:
            ep = await asyncio.to_thread(provider.get_episode, page_url, True)

            available = {
                str(source.get("label", "")).lower(): source
                for source in (ep.get("sources") or [])
            }
            if label.lower() not in available:
                raise ProviderError(
                    "Requested quality is not available. Available: "
                    + (", ".join(sorted(available)) or "none")
                )

            await download_and_send(
                ep,
                query.message.chat.id,
                preferred_quality=label,
                status=status,
            )
            await status.delete()
        except Exception as exc:
            await safe_edit(
                status,
                "❌ <b>Download failed</b>\n" + html.escape(str(exc)),
            )

    except Exception as exc:
        await query.message.reply_text("❌ " + html.escape(str(exc)))


def scheduler_loop():
    from crawler.scheduler import check_once

    if INITIAL_CRAWL_PAGES:
        try:
            print(
                "[scheduler] initial crawl: {} page(s)".format(INITIAL_CRAWL_PAGES),
                flush=True,
            )
            with BACKGROUND_LOCK:
                from crawler.catalog import crawl
                crawl(INITIAL_CRAWL_PAGES, 0.25)
        except Exception as exc:
            print("[scheduler] initial crawl failed: {}".format(exc), flush=True)

    while True:
        try:
            print("[scheduler] checking first {} page(s)...".format(CRAWL_PAGES), flush=True)
            with BACKGROUND_LOCK:
                added = check_once(CRAWL_PAGES)
                published = publish_pending(PUBLISH_LIMIT)
            print(
                "[scheduler] added={}, published={}".format(added, published),
                flush=True,
            )
        except Exception as exc:
            print("[scheduler] check failed: {}".format(exc), flush=True)

        time.sleep(CRAWL_INTERVAL)


def start_background_scheduler():
    if not SCHEDULER_ENABLED:
        print("[scheduler] disabled", flush=True)
        return

    thread = threading.Thread(
        target=scheduler_loop,
        name="catalog-scheduler",
        daemon=True,
    )
    thread.start()
    print(
        "[scheduler] enabled: every {}s, pages={}, publish_limit={}".format(
            CRAWL_INTERVAL, CRAWL_PAGES, PUBLISH_LIMIT
        ),
        flush=True,
    )


async def main():
    print("WHBot starting", flush=True)
    load_download_channel_setting()
    print("[telegram] configured download channel: {}".format(DOWNLOAD_CHANNEL_ID or "NOT SET"), flush=True)

    # app.run(main) owns the event loop. Start and stop Pyrogram on that
    # same loop so peer resolution and dispatcher shutdown use one loop.
    await app.start()
    try:
        await initialize_telegram_peers()
        start_background_scheduler()
        await idle()
    finally:
        await app.stop()

if __name__ == "__main__":
    app.run(main)
