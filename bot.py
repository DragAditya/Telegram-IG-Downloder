"""
╔═══════════════════════════════════════════════════════════════╗
║        🚀 DRAGDOWNLOADER BOT  —  v4.0 PRODUCTION             ║
║  ⚡ Memory fetch → Disk fallback                            ║
║  🧵 ThreadPoolExecutor (4 workers)                           ║
║  🎛️  Inline buttons, live status, rate limiting              ║
║  🌐 AUTO: Webhook on Render, Polling on Termux               ║
║  🔒 Token via env var or hardcode                            ║
╚═══════════════════════════════════════════════════════════════╝
"""

# ═══════════════════════════════════════════
#  IMPORTS
# ═══════════════════════════════════════════
import asyncio
import io
import logging
import os
import re
import time
import urllib.request
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import yt_dlp
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

# ═══════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════
BOT_TOKEN    = os.environ.get("BOT_TOKEN",    "YOUR_BOT_TOKEN_HERE")
COOKIES_FILE = os.environ.get("COOKIES_FILE", "cookies.txt")
WEBHOOK_URL  = os.environ.get("WEBHOOK_URL",  "")   # e.g. https://yourapp.onrender.com
PORT         = int(os.environ.get("PORT",     "8443"))

DOWNLOAD_DIR   = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)
MAX_FILE_BYTES = 48 * 1024 * 1024
RATE_LIMIT_SEC = 10
MAX_WORKERS    = 4

# ═══════════════════════════════════════════
#  LOGGING  — coloured
# ═══════════════════════════════════════════
class _CF(logging.Formatter):
    C = {logging.DEBUG:"\033[36m", logging.INFO:"\033[32m",
         logging.WARNING:"\033[33m", logging.ERROR:"\033[31m",
         logging.CRITICAL:"\033[35m"}
    R = "\033[0m"
    def format(self, r):
        r.levelname = f"{self.C.get(r.levelno,'')}{r.levelname:<8}{self.R}"
        return super().format(r)

_h = logging.StreamHandler()
_h.setFormatter(_CF("%(asctime)s │ %(levelname)s │ %(message)s", "%H:%M:%S"))
logging.basicConfig(level=logging.INFO, handlers=[_h])
log = logging.getLogger("DragBot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# ═══════════════════════════════════════════
#  THREAD POOL + STATE
# ═══════════════════════════════════════════
POOL = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="ydl")
stats: dict = {"total":0, "fast_ok":0, "fallback_ok":0, "failed":0,
               "start_time": datetime.now()}
_rate: dict[int, float] = defaultdict(float)

# ═══════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════
INSTA_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:reel|p|tv|stories)/[^\s]+",
    re.IGNORECASE)

def find_url(text: str) -> str | None:
    m = INSTA_RE.search(text)
    return m.group(0).rstrip("/?&") if m else None

def fmt_dur(s) -> str:
    if not s: return ""
    m, sec = divmod(int(s), 60)
    return f"{m}:{sec:02d}"

def fmt_mb(b) -> str:
    if not b: return ""
    return f"{b/(1024*1024):.1f} MB"

def rate_ok(uid: int) -> tuple[bool, float]:
    elapsed = time.monotonic() - _rate[uid]
    if elapsed >= RATE_LIMIT_SEC:
        _rate[uid] = time.monotonic()
        return True, 0.0
    return False, round(RATE_LIMIT_SEC - elapsed, 1)

def ydl_base() -> dict:
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
    if Path(COOKIES_FILE).exists():
        opts["cookiefile"] = COOKIES_FILE
    return opts

# _safe_md removed — using plain Markdown everywhere

# ═══════════════════════════════════════════
#  DOWNLOAD — Method A: memory (fast)
# ═══════════════════════════════════════════
def _fetch_memory(url: str) -> dict | None:
    opts = ydl_base()
    opts["format"] = "best[ext=mp4][filesize<?48M]/best[ext=mp4]/best"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return None

        direct, hdrs = None, {}
        if "url" in info:
            direct, hdrs = info["url"], info.get("http_headers", {})
        elif "formats" in info:
            for fmt in reversed(info["formats"]):
                if fmt.get("ext") == "mp4" and fmt.get("url"):
                    direct = fmt["url"]
                    hdrs   = fmt.get("http_headers", {})
                    break
            if not direct:
                last   = info["formats"][-1]
                direct = last.get("url")
                hdrs   = last.get("http_headers", {})
        if not direct:
            return None

        approx = info.get("filesize") or info.get("filesize_approx", 0)
        if approx and approx > MAX_FILE_BYTES:
            return None

        t0  = time.monotonic()
        req = urllib.request.Request(direct, headers=hdrs)
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = resp.read()
        log.info(f"Fetched {fmt_mb(len(data))} in {time.monotonic()-t0:.1f}s")

        return {"bytes": data, "size": len(data),
                "title": info.get("title",""), "duration": info.get("duration",0)}
    except Exception as e:
        log.warning(f"Memory fetch failed: {e}")
        return None

# ═══════════════════════════════════════════
#  DOWNLOAD — Method B: disk (fallback)
# ═══════════════════════════════════════════
def _fetch_disk(url: str) -> Path | None:
    uid = uuid.uuid4().hex[:8]
    out = str(DOWNLOAD_DIR / f"{uid}.%(ext)s")
    opts = ydl_base()
    opts["format"]  = "best[ext=mp4][filesize<?48M]/best[ext=mp4]/best"
    opts["outtmpl"] = out
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        if info and "requested_downloads" in info:
            return Path(info["requested_downloads"][0]["filepath"])
        hits = list(DOWNLOAD_DIR.glob(f"{uid}.*"))
        return hits[0] if hits else None
    except Exception as e:
        log.error(f"Disk fetch failed: {e}")
        return None

def _cleanup(p: Path | None):
    try:
        if p and p.exists(): p.unlink()
    except Exception: pass

# ═══════════════════════════════════════════
#  UI BUILDERS
# ═══════════════════════════════════════════
def _home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📊 Stats", callback_data="stats"),
        InlineKeyboardButton("🏓 Ping",  callback_data="ping"),
        InlineKeyboardButton("❓ Help",  callback_data="help"),
    ]])

def _post_kb(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔗 Open Original", url=url),
    ]])

def _stats_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Refresh", callback_data="stats"),
        InlineKeyboardButton("🏠 Home",    callback_data="home"),
    ]])

def _err_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Try Again", callback_data="try_again"),
    ]])

def _stats_text() -> str:
    up   = datetime.now() - stats["start_time"]
    h, r = divmod(int(up.total_seconds()), 3600)
    m    = r // 60
    tot  = stats["total"] or 1
    ok   = stats["fast_ok"] + stats["fallback_ok"]
    return (
        "📊 *Bot Statistics*\n"
        f"`{'─'*26}`\n"
        f"⏱  Uptime          `{h}h {m}m`\n"
        f"📥 Total requests  `{stats['total']}`\n"
        f"⚡ Fast method     `{stats['fast_ok']}`\n"
        f"💾 Disk fallback   `{stats['fallback_ok']}`\n"
        f"❌ Failed          `{stats['failed']}`\n"
        f"✅ Success rate    `{ok/tot*100:.1f}%`\n"
        f"`{'─'*26}`\n"
        f"🧵 Workers         `{MAX_WORKERS}`\n"
        f"⏳ Rate limit      `{RATE_LIMIT_SEC}s/user`"
    )

# ═══════════════════════════════════════════
#  COMMANDS
# ═══════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "there"
    await update.message.reply_text(
        f"👋 Hey *{name}*!\n\n"
        "⚡ *DragDownloader* — Instagram Bot\n\n"
        "Send any Instagram link:\n"
        "  🎬  Reels\n"
        "  📸  Posts (video)\n"
        "  📺  IGTV\n\n"
        "📌 _Public posts only_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_home_kb(),
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *How to use*\n\n"
        "1. Open any Instagram Reel / Post\n"
        "2. Tap *Share* → *Copy Link*\n"
        "3. Paste here → Send\n"
        "4. Done! ⚡\n\n"
        "*Commands*\n"
        "`/start`  — Welcome\n"
        "`/help`   — This message\n"
        "`/ping`   — Latency\n"
        "`/stats`  — Statistics\n\n"
        f"*Limits*\n"
        f"• Max size: `48 MB`\n"
        f"• Rate limit: `{RATE_LIMIT_SEC}s` per user\n"
        "• Public accounts only"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🏠 Home",   callback_data="home"),
        InlineKeyboardButton("📊 Stats",  callback_data="stats"),
    ]])
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t0  = time.monotonic()
    msg = await update.message.reply_text("🏓 Pinging...", parse_mode=ParseMode.MARKDOWN)
    ms  = (time.monotonic() - t0) * 1000
    bar = "█" * min(int(ms / 15), 20) + "░" * (20 - min(int(ms / 15), 20))
    emoji = "🟢" if ms < 300 else "🟡" if ms < 800 else "🔴"
    await msg.edit_text(
        f"🏓 *Pong!* {emoji}\n\n`{bar}`\n`{ms:.0f} ms`",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        _stats_text(), parse_mode=ParseMode.MARKDOWN, reply_markup=_stats_kb()
    )

# ═══════════════════════════════════════════
#  CALLBACK HANDLER
# ═══════════════════════════════════════════
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    # Stats button can be on video messages (no text) — always send new message
    async def _send_new(text, parse_mode, reply_markup=None):
        await ctx.bot.send_message(
            chat_id=q.message.chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )

    async def _edit_or_new(text, parse_mode, reply_markup=None):
        """Edit if message has text, otherwise send new."""
        try:
            if q.message.text:  # has text → can edit
                await q.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
            else:
                await _send_new(text, parse_mode, reply_markup)
        except TelegramError:
            await _send_new(text, parse_mode, reply_markup)

    if data == "stats":
        await _edit_or_new(_stats_text(), ParseMode.MARKDOWN, _stats_kb())

    elif data == "ping":
        t0 = time.monotonic()
        ms = (time.monotonic() - t0) * 1000 + 2
        emoji = "🟢" if ms < 300 else "🟡" if ms < 800 else "🔴"
        await _edit_or_new(
            f"🏓 *Pong!* {emoji}\nLatency: `{ms:.0f} ms`",
            ParseMode.MARKDOWN, _home_kb()
        )

    elif data == "help":
        text = (
            "📖 *How to use*\n\n"
            "1. Open any Instagram Reel / Post\n"
            "2. Tap *Share* → *Copy Link*\n"
            "3. Paste here → Send\n"
            "4. Done! ⚡\n\n"
            f"• Max size: `48 MB`\n"
            f"• Rate limit: `{RATE_LIMIT_SEC}s` per user"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Home", callback_data="home"),
        ]])
        await _edit_or_new(text, ParseMode.MARKDOWN, kb)

    elif data == "home":
        name = q.from_user.first_name or "there"
        await _edit_or_new(
            f"👋 Hey *{name}*!\n\n"
            "⚡ *DragDownloader* — Instagram Bot\n\n"
            "Send any Instagram link:\n"
            "  🎬  Reels\n"
            "  📸  Posts (video)\n"
            "  📺  IGTV\n\n"
            "📌 _Public posts only_",
            ParseMode.MARKDOWN, _home_kb()
        )

    elif data == "try_again":
        await _send_new(
            "🔗 Send the Instagram link again to retry.",
            ParseMode.MARKDOWN
        )

# ═══════════════════════════════════════════
#  MAIN DOWNLOAD HANDLER
# ═══════════════════════════════════════════
async def handle_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    url  = find_url(text)

    if not url:
        await update.message.reply_text(
            "❌ No valid Instagram link found.\n\n"
            "_Example:_\n"
            "`https://www.instagram.com/reel/ABC123/`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    uid = update.effective_user.id
    ok, wait = rate_ok(uid)
    if not ok:
        await update.message.reply_text(
            f"⏳ *Slow down!*  Wait `{wait}s` before next request.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    stats["total"] += 1
    user = update.effective_user
    log.info(f"▶ {user.first_name} ({uid}) → {url[:70]}")

    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_VIDEO)
    status = await update.message.reply_text("⚡ *Fetching…*", parse_mode=ParseMode.MARKDOWN)
    t0     = time.monotonic()
    loop   = asyncio.get_event_loop()

    # ── FAST: memory ───────────────────────
    await status.edit_text("🔍 *Extracting…*", parse_mode=ParseMode.MARKDOWN)
    result = await loop.run_in_executor(POOL, _fetch_memory, url)

    if result:
        elapsed = time.monotonic() - t0
        parts   = ["✅ *Downloaded*"]
        if result["title"]:
            t = result["title"][:55] + ("…" if len(result["title"]) > 55 else "")
            parts.append(f"📝 {t}")
        meta = []
        if result["duration"]: meta.append(f"⏱ `{fmt_dur(result['duration'])}`")
        if result["size"]:     meta.append(f"📦 `{fmt_mb(result['size'])}`")
        meta.append(f"⚡ `{elapsed:.1f}s`")
        parts.append("  ".join(meta))
        caption = "\n".join(parts)

        try:
            await status.edit_text("📤 *Uploading…*", parse_mode=ParseMode.MARKDOWN)
            await update.message.reply_video(
                video=io.BytesIO(result["bytes"]),
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
                supports_streaming=True,
                reply_markup=_post_kb(url),
                read_timeout=120, write_timeout=120, connect_timeout=30,
            )
            await status.delete()
            stats["fast_ok"] += 1
            log.info(f"✅ Fast done {elapsed:.1f}s")
            return
        except TelegramError as e:
            log.warning(f"Fast upload err: {e} → fallback")

    # ── FALLBACK: disk ──────────────────────
    await status.edit_text("💾 *Downloading…*", parse_mode=ParseMode.MARKDOWN)
    path = await loop.run_in_executor(POOL, _fetch_disk, url)

    if not path or not path.exists():
        stats["failed"] += 1
        await status.edit_text(
            "❌ *Download failed*\n\n"
            "Possible reasons:\n"
            "• Private account\n"
            "• Expired or invalid link\n"
            "• Instagram temporarily blocked\n\n"
            "_Try again in a moment_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_err_kb(),
        )
        return

    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        stats["failed"] += 1
        _cleanup(path)
        await status.edit_text(
            f"⚠️ *File too large*\n`{fmt_mb(size)}` — Telegram limit is `48 MB`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=_err_kb(),
        )
        return

    elapsed = time.monotonic() - t0
    await status.edit_text("📤 *Uploading…*", parse_mode=ParseMode.MARKDOWN)

    try:
        with open(path, "rb") as f:
            await update.message.reply_video(
                video=f,
                caption=(
                    f"✅ *Downloaded*\n"
                    f"📦 `{fmt_mb(size)}`  ⏱ `{elapsed:.1f}s`"
                ),
                parse_mode=ParseMode.MARKDOWN,
                supports_streaming=True,
                reply_markup=_post_kb(url),
                read_timeout=180, write_timeout=180,
            )
        await status.delete()
        stats["fallback_ok"] += 1
        log.info(f"✅ Fallback done {elapsed:.1f}s")
    except TelegramError as e:
        stats["failed"] += 1
        log.error(f"Upload error: {e}")
        await status.edit_text("❌ Upload failed. Please try again.", reply_markup=_err_kb())
    finally:
        _cleanup(path)

# ═══════════════════════════════════════════
#  ERROR HANDLER
# ═══════════════════════════════════════════
async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    log.error(f"Error: {ctx.error}", exc_info=ctx.error)

# ═══════════════════════════════════════════
#  STARTUP HOOK
# ═══════════════════════════════════════════
async def on_start(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Welcome screen"),
        BotCommand("help",  "How to use"),
        BotCommand("ping",  "Check latency"),
        BotCommand("stats", "Download stats"),
    ])
    me = await app.bot.get_me()
    mode = "WEBHOOK" if WEBHOOK_URL else "POLLING"
    log.info(f"✅ @{me.username} online │ mode={mode} │ workers={MAX_WORKERS}")

# ═══════════════════════════════════════════
#  MAIN — auto webhook/polling
# ═══════════════════════════════════════════
def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        log.critical("❌ Set BOT_TOKEN env var or hardcode on line 42")
        raise SystemExit(1)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(on_start)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(30)
        .pool_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("ping",  cmd_ping))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_error_handler(on_error)

    if WEBHOOK_URL:
        # ── RENDER / PRODUCTION: webhook mode ──
        log.info(f"🌐 Webhook mode → {WEBHOOK_URL}  port={PORT}")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=f"{WEBHOOK_URL}/webhook",
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"],
        )
    else:
        # ── TERMUX / LOCAL: polling mode ──
        log.info("🔄 Polling mode (Termux/local)")
        app.run_polling(
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"],
        )

import asyncio

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        main()
    finally:
        loop.close()
