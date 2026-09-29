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


PROGRESS_UPDATE_INTERVAL = max(
    float(os.getenv("DOWNLOAD_PROGRESS_INTERVAL", "2.5")),
    2.5,
)


async def safe_edit(message, text):
    """Safely edit a Telegram status message without breaking the transfer."""
    if message is None or not text:
        return

    current = getattr(message, "text", None) or getattr(message, "caption", None)
    if current == text:
        return

    try:
        await message.edit_text(text)
    except FloodWait as exc:
        wait = max(int(getattr(exc, "value", 0) or 0), 1)
        print("[progress] FloodWait={}s".format(wait), flush=True)
        await asyncio.sleep(wait)
        try:
            await message.edit_text(text)
        except Exception as retry_exc:
            print("[progress] edit retry failed: {}".format(retry_exc), flush=True)
    except RPCError as exc:
        # A progress update must never abort an otherwise healthy download.
        print("[progress] edit skipped: {}".format(exc), flush=True)
    except Exception as exc:
        print("[progress] edit failed: {}".format(exc), flush=True)


def _progress_bar(percent, width=14):
    percent = max(0.0, min(float(percent), 100.0))
    filled = int(round(width * percent / 100.0))
    return "█" * filled + "░" * (width - filled)


def _format_rate(bytes_per_second):
    if bytes_per_second <= 0:
        return "0 B/s"
    units = ("B/s", "KB/s", "MB/s", "GB/s")
    value = float(bytes_per_second)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return "{:.0f} {}".format(value, unit) if unit == "B/s" else "{:.1f} {}".format(value, unit)
        value /= 1024.0
    return "0 B/s"


def _format_eta(seconds):
    if seconds is None or seconds < 0:
        return "Unknown"
    seconds = int(seconds)
    if seconds < 60:
        return "{}s".format(seconds)
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return "{}m {}s".format(minutes, secs)
    hours, minutes = divmod(minutes, 60)
    return "{}h {}m".format(hours, minutes)


def series_transfer_text(
    series_name,
    stage,
    episode_index,
    episode_total,
    completed_count,
    current_bytes,
    total_bytes,
    speed,
    eta=None,
):
    """Build the single-message download/upload status used by series jobs."""
    episode_total = max(int(episode_total or 1), 1)
    episode_index = max(min(int(episode_index or 1), episode_total), 1)
    completed_count = max(min(int(completed_count or 0), episode_total), 0)
    total_bytes = int(total_bytes or 0)
    current_bytes = max(int(current_bytes or 0), 0)

    if total_bytes > 0:
        percent = min(current_bytes * 100.0 / total_bytes, 100.0)
    else:
        percent = 0.0

    stage_label = "📥 DOWNLOAD" if stage == "download" else "☁️ UPLOAD"
    eta_text = _format_eta(eta)
    progress_line = (
        "[{}] {:.0f}%".format(_progress_bar(percent), percent)
    )
    size_line = (
        "{} / {}".format(
            _format_bytes(current_bytes),
            _format_bytes(total_bytes),
        )
        if total_bytes > 0
        else _format_bytes(current_bytes)
    )

    progress = []
    for number in range(1, episode_total + 1):
        if number <= completed_count:
            progress.append("{} ✅".format(number))
        elif number == episode_index:
            progress.append("{} 🔄".format(number))
        else:
            progress.append("{} ⏳".format(number))

    return (
        "🎬 <b>{}</b>\n\n"
        "{}\n"
        "Episode {} / {}\n"
        "{}\n"
        "📦 {}\n"
        "⚡ {}  •  ETA {}\n\n"
        "📊 <b>Progress</b>\n"
        "{}"
    ).format(
        html.escape(str(series_name)),
        stage_label,
        episode_index,
        episode_total,
        progress_line,
        size_line,
        _format_rate(speed),
        eta_text,
        "  ".join(progress),
    )


def _format_bytes(value):
    value = max(float(value or 0), 0.0)
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return "{:.0f} {}".format(value, unit)
            return "{:.1f} {}".format(value, unit)
        value /= 1024.0
    return "0 B"


def _make_transfer_progress(status, series_name, stage, episode_index, episode_total, completed_count):
    state = {"last": 0.0, "last_text": ""}

    def progress(current, total, started):
        if status is None:
            return

        now = time.monotonic()
        # The callbacks run in worker threads. Throttle Telegram edits here,
        # while allowing the final 100% callback through immediately.
        if now - state["last"] < PROGRESS_UPDATE_INTERVAL and not (
            total and current >= total
        ):
            return

        elapsed = max(now - started, 0.001)
        speed = current / elapsed
        eta = ((total - current) / speed) if total and speed > 0 else None
        text_value = series_transfer_text(
            series_name,
            stage,
            episode_index,
            episode_total,
            completed_count,
            current,
            total,
            speed,
            eta,
        )
        if text_value == state["last_text"]:
            return

        state["last"] = now
        state["last_text"] = text_value
        try:
            future = asyncio.run_coroutine_threadsafe(
                safe_edit(status, text_value),
                app.loop,
            )
            # Do not wait on the Telegram API from the downloader/uploader thread.
            future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        except Exception as exc:
            print("[progress] scheduling failed: {}".format(exc), flush=True)

    return progress


def download_progress_factory(status, series_name="Video", episode_index=1, episode_total=1, completed_count=0):
    return _make_transfer_progress(
        status,
        series_name,
        "download",
        episode_index,
        episode_total,
        completed_count,
    )


def gofile_progress_factory(status, series_name="Video", episode_index=1, episode_total=1, completed_count=0):
    return _make_transfer_progress(
        status,
        series_name,
        "upload",
        episode_index,
        episode_total,
        completed_count,
    )


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
    series_episode_index=None,
    series_completed=0,
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
        await safe_edit(
            status,
            series_transfer_text(
                series_name or ep.get("title") or "Video",
                "download",
                series_episode_index or 1,
                series_total or 1,
                series_completed,
                0,
                0,
                0,
            ),
        )

    try:
        await asyncio.to_thread(
            provider.download,
            source["url"],
            output,
            download_progress_factory(
                status,
                series_name=series_name or ep.get("title") or "Video",
                episode_index=series_episode_index or 1,
                episode_total=series_total or 1,
                completed_count=series_completed,
            ) if status else None,
            ep.get("page_url"),
        )

        size = output.stat().st_size
        print("[download] completed local file size={}".format(size), flush=True)

        metadata = await asyncio.to_thread(video_metadata, output)
        print("[download] ffprobe duration={} width={} height={}".format(
            metadata.get("duration"), metadata.get("width"), metadata.get("height")
        ), flush=True)

        if not upload_to_gofile:
            retained_output = True
            return {"message": None, "path": output, "metadata": metadata}

        if status:
            await safe_edit(
                status,
                series_transfer_text(
                    series_name or ep.get("title") or "Video",
                    "upload",
                    series_episode_index or 1,
                    series_total or 1,
                    series_completed,
                    0,
                    0,
                    0,
                ),
            )

        # Guest upload: no API token is supplied. GoFile creates a temporary
        # guest account and returns a public downloadPage for this file.
        uploader = GofileUploader()
        started = time.monotonic()
        gofile_url = await asyncio.to_thread(
            uploader.upload,
            output,
            gofile_progress_factory(
                status,
                series_name=series_name or ep.get("title") or "Video",
                episode_index=series_episode_index or 1,
                episode_total=series_total or 1,
                completed_count=series_completed,
            ),
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


@app.on_message(filters.private, group=-2)
async def bootstrap_channel_from_forward(_, message):
    """Learn/cache a channel peer from a directly forwarded channel post.

    Telegram bots cannot enumerate dialogs, so a fresh Pyrogram session may
    not know a private channel from its numeric ID alone. A direct forward
    from the target channel contains the channel peer and lets Pyrogram cache
    it. This is intentionally a non-command handler so /start and /download
    remain the only bot commands.
    """
    if not owner_only(message):
        return

    forwarded_chat = getattr(message, "forward_from_chat", None)

    # Pyrogram 2 can expose newer forward-origin objects on some updates.
    if forwarded_chat is None:
        origin = getattr(message, "forward_origin", None)
        forwarded_chat = getattr(origin, "chat", None)

    if not forwarded_chat:
        return

    chat_type = (
        getattr(getattr(forwarded_chat, "type", None), "value", None)
        or str(getattr(forwarded_chat, "type", ""))
    )
    if chat_type != "channel":
        return

    channel_id = getattr(forwarded_chat, "id", None)
    if not channel_id:
        return

    print(
        "[telegram setup] forwarded channel detected id={} title={}".format(
            channel_id,
            getattr(forwarded_chat, "title", None)
            or getattr(forwarded_chat, "username", None)
            or "unknown",
        ),
        flush=True,
    )

    # The forwarded update itself gives Telegram the channel peer. Explicitly
    # resolve it before saving so later uploads can use the cached peer.
    try:
        await app.resolve_peer(channel_id)
        save_download_channel_setting(channel_id)
        print(
            "[telegram setup] channel peer cached and saved: {}".format(
                channel_id
            ),
            flush=True,
        )

        if await validate_download_channel():
            await message.reply_text(
                "✅ <b>Download channel connected.</b>\n\n"
                "📥 Channel: <b>{}</b>\n"
                "🆔 ID: <code>{}</code>".format(
                    html.escape(
                        str(
                            getattr(forwarded_chat, "title", None)
                            or getattr(forwarded_chat, "username", None)
                            or "Unknown"
                        )
                    ),
                    channel_id,
                )
            )
        else:
            await message.reply_text(
                "⚠️ I received the channel post, but Telegram still did not "
                "allow the bot to resolve the channel. Make sure this bot is "
                "still an administrator in that channel."
            )
    except Exception as exc:
        print(
            "[telegram setup] forwarded channel bootstrap failed: {}".format(
                exc
            ),
            flush=True,
        )


@app.on_message(filters.command("start"))
async def start(_, message):
    await message.reply_text(
        "WHBot ready.\n\n"
        "/start - show this message\n"
        "/download <series URL> - download a complete series"
    )

def extract_series_url(text):
    """Return the first valid WatchHentai series URL found in arbitrary text."""
    if not text:
        return None
    match = re.search(
        r"https?://watchhentai\\.net/series/[^\\s<>\\\"']+",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    return match.group(0).rstrip(".,!?)]}>")


@app.on_message(filters.text, group=-2)
async def auto_detect_series_link(_, message):
    """Automatically start the normal series workflow when a series URL is pasted."""
    text_value = getattr(message, "text", None) or ""
    if not text_value or text_value.startswith("/"):
        return

    # A pending /download URL or another workflow should keep its existing
    # behavior. This handler only handles a directly pasted series link.
    if DOWNLOAD_STATES.get(message.chat.id) or CRAWL_STATES.get(message.chat.id):
        return

    series_url = extract_series_url(text_value)
    if not series_url:
        return

    print(
        "[auto-detect] series URL detected chat_id={} url={}".format(
            message.chat.id, series_url
        ),
        flush=True,
    )
    await start_series_download(message, series_url)


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
        "⬇️ Download episodes individually, or download every episode to GoFile."
    )
    markup = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "⬇️ Download All Episodes",
                callback_data="sd:" + enc(series["series_url"]),
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
        # Keep the existing single series menu, but use the series artwork as
        # the visual thumbnail for the GoFile episode links.
        thumbnail = series.get("thumbnail")
        if thumbnail:
            try:
                await app.send_photo(
                    chat_id,
                    thumbnail,
                    caption=text,
                    reply_markup=markup,
                )
                return
            except Exception as exc:
                print(
                    "[series menu] thumbnail failed: {}".format(exc),
                    flush=True,
                )
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
        status = await message.reply_text("🎬 <b>Preparing download...</b>")

    try:
        series, episode_items = await resolve_series(series_url)
        total = series.get("total_episodes") or len(episode_items)
        await safe_edit(status, "🎬 <b>{}</b>".format(html.escape(series["name"])))

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
                print(
                    "[series download] trying episode {} quality={}".format(
                        index, label
                    ),
                    flush=True,
                )
                try:
                    result = await download_and_send(
                        ep,
                        upload_chat,
                        preferred_quality=label,
                        status=status,
                        series_name=series["name"],
                        series_total=total,
                        series_episode_index=index,
                        series_completed=index - 1,
                        send_telegram=False,
                    )
                    if result.get("gofile_url"):
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
            "🎬 <b>{}</b>\n\n"
            "✅ <b>COMPLETE</b>\n\n"
            "📊 <b>Progress</b>\n"
            "{}".format(
                html.escape(series["name"]),
                "  ".join("{} ✅".format(i) for i in range(1, total + 1)),
            ),
        )
        await send_series_episode_menu(upload_chat, series, series_episode_results)

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




@app.on_callback_query()
async def callback(_, query):
    print("[callback] data={}".format(query.data[:80]), flush=True)
    await query.answer()

    try:
        if query.data.startswith("sd:"):
            series_url = dec(query.data[3:])
            if AUTO_LOCK.locked():
                await query.message.reply_text(
                    "⏳ Another download/upload job is already running."
                )
                return

            status = await query.message.reply_text("🎬 <b>Preparing download...</b>")
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

    # app.run(main()) runs this coroutine on Pyrogram's event loop. Start and
    # stop Pyrogram on that same loop so peer resolution and shutdown are clean.
    await app.start()
    try:
        await initialize_telegram_peers()
        start_background_scheduler()
        await idle()
    finally:
        await app.stop()

if __name__ == "__main__":
    app.run(main())
