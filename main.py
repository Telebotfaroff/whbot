import asyncio,base64,os
from pathlib import Path
from dotenv import load_dotenv
from telegram import InlineKeyboardButton,InlineKeyboardMarkup,Update
from telegram.ext import Application,CallbackQueryHandler,CommandHandler,ContextTypes
from providers.watchhentai import WatchHentai

load_dotenv()
TOKEN=os.getenv("BOT_TOKEN")
if not TOKEN: raise RuntimeError("BOT_TOKEN is required")
DOWNLOAD_DIR=Path(os.getenv("DOWNLOAD_DIR","./downloads")); DOWNLOAD_DIR.mkdir(parents=True,exist_ok=True)
MAX_UPLOAD_MB=float(os.getenv("MAX_UPLOAD_MB","50"))
provider=WatchHentai()

def enc(v): return base64.urlsafe_b64encode(v.encode()).decode().rstrip("=")
def dec(v): return base64.urlsafe_b64decode(v+"="*(-len(v)%4)).decode()

def keyboard(ep):
    rows=[]; nav=[]
    n=ep["navigation"]
    if n.get("previous"): nav.append(InlineKeyboardButton("⬅ Previous",callback_data="ep:"+enc(n["previous"])))
    if n.get("series"): nav.append(InlineKeyboardButton("📋 All Episodes",url=n["series"]))
    if n.get("next"): nav.append(InlineKeyboardButton("Next ➡",callback_data="ep:"+enc(n["next"])))
    if nav: rows.append(nav)
    q=[InlineKeyboardButton("⬇ "+s["label"],callback_data="dl:"+enc(ep["page_url"])+":"+enc(s["label"])) for s in ep["sources"]]
    if q: rows.append(q)
    return InlineKeyboardMarkup(rows)

def text(ep):
    return f"🎬 <b>{ep['title']}</b>\nEpisode: <b>{ep.get('episode') or '?'}</b>\n\nAvailable: "+(", ".join(s["label"] for s in ep["sources"]) or "none")

async def show(update,ep):
    msg=update.effective_message
    try:
        if ep.get("thumbnail"):
            await msg.reply_photo(ep["thumbnail"],caption=text(ep),parse_mode="HTML",reply_markup=keyboard(ep)); return
    except Exception: pass
    await msg.reply_text(text(ep),parse_mode="HTML",reply_markup=keyboard(ep))

async def start(update,ctx): await update.message.reply_text("WHBot ready.\n\n/latest\n/episode <URL>")

async def latest(update,ctx):
    try:
        await update.message.reply_text("🔎 Loading homepage...")
        for item in (await asyncio.to_thread(provider.latest,1))[:10]:
            await show(update,await asyncio.to_thread(provider.get_episode,item["page_url"],True))
    except Exception as e: await update.message.reply_text("❌ "+str(e))

async def episode(update,ctx):
    if not ctx.args: return await update.message.reply_text("Usage: /episode <URL>")
    try: await show(update,await asyncio.to_thread(provider.get_episode," ".join(ctx.args),True))
    except Exception as e: await update.message.reply_text("❌ "+str(e))

async def callback(update,ctx):
    q=update.callback_query; await q.answer()
    try:
        if q.data.startswith("ep:"):
            await show(update,await asyncio.to_thread(provider.get_episode,dec(q.data[3:]),True)); return
        if q.data.startswith("dl:"):
            a,b=q.data[3:].split(":",1); page=dec(a); label=dec(b)
            ep=await asyncio.to_thread(provider.get_episode,page,True)
            source=next((s for s in ep["sources"] if s["label"]==label),None)
            if not source: raise RuntimeError("Source unavailable")
            safe="".join(c if c.isalnum() or c in "._-" else "_" for c in ep["title"])[:80]
            output=DOWNLOAD_DIR/f"{safe}-{label}.mp4"
            status=await q.message.reply_text("⬇️ Downloading "+label+"...")
            await asyncio.to_thread(provider.download,source["url"],output)
            size=output.stat().st_size/1048576
            if size>MAX_UPLOAD_MB:
                return await status.edit_text(f"✅ Downloaded {size:.1f} MB.\n⚠️ Exceeds MAX_UPLOAD_MB={MAX_UPLOAD_MB:g}.")
            await status.edit_text("📤 Uploading...")
            with output.open("rb") as f: await q.message.reply_video(f,caption=f"{ep['title']} — {label}",supports_streaming=True)
            await status.delete()
    except Exception as e: await q.message.reply_text("❌ "+str(e))

def main():
    app=Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",start)); app.add_handler(CommandHandler("latest",latest)); app.add_handler(CommandHandler("episode",episode)); app.add_handler(CallbackQueryHandler(callback))
    print("WHBot running"); app.run_polling()

if __name__=="__main__": main()
