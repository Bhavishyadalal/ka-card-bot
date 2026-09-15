#!/usr/bin/env python3
"""
Kingdom Archives — Telegram Bot
Playercard search, preview, and download
"""

import asyncio
import os, re, random, logging, threading, time
from io import BytesIO

import requests
from bs4 import BeautifulSoup
from flask import Flask
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)
from telegram.constants import ParseMode

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CONFIG
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TOKEN    = os.environ.get("BOT_TOKEN", "")
SITE     = "https://kingdomarchives.com"
BASE_URL = f"{SITE}/playercards"
IMG_ROOT = f"{SITE}/uploads/playercards/"
PORT     = int(os.environ.get("PORT", 10000))

KNOWN_SUBS = [
    "default", "skins", "battlepass", "drops",
    "vct2026", "agents", "eventpass", "vct", "premier",
]

SUB_LABELS = {
    "default":    "🃏 Default",
    "skins":      "✨ Skins",
    "battlepass": "🎯 Battlepass",
    "drops":      "📦 Drops",
    "vct2026":    "🏆 VCT 2026",
    "agents":     "🕵️ Agents",
    "eventpass":  "🎪 Event Pass",
    "vct":        "🏅 VCT",
    "premier":    "👑 Premier",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Mobile Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Referer": f"{SITE}/",
}

SIZES = ["large", "wide", "small"]
SIZE_LABELS = {"large": "🖼 Large", "wide": "📐 Wide", "small": "🔹 Small"}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CACHE  (in-memory, refreshed every 30 min – non-blocking)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_cache: list[dict] = []          # [{name, sub, display, desc}, ...]
_cache_ts: float   = 0
CACHE_TTL = 1800                 # 30 minutes
_refresh_lock = threading.Lock()
_refreshing = False

http = requests.Session()
http.headers.update(HEADERS)

def _get_soup(url: str) -> BeautifulSoup:
    r = http.get(url, timeout=20)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")

def _scrape_page(url: str) -> list[dict]:
    cards = []
    try:
        soup = _get_soup(url)
        for img in soup.find_all("img", attrs={"data-card-type": True}):
            sub   = img.get("data-card-type", "default").strip()
            large = img.get("data-large", "")
            name  = re.sub(r'(?:_large|_wide|_small)\.(?:png|jpg|webp)$', '', large, flags=re.I)
            if not name:
                src = img.get("src", "")
                m = re.search(r'/playercards/[^/]+/(.+?)(?:_large|_wide|_small)\.png', src)
                name = m.group(1) if m else ""
            if not name:
                continue
            display = img.get("data-name", name.replace("-", " ").replace("_", " "))
            desc    = img.get("data-description", "")
            cards.append({"name": name, "sub": sub, "display": display, "desc": desc})
    except Exception as e:
        log.warning(f"Scrape error on {url}: {e}")
    return cards

def _next_page_url(soup: BeautifulSoup, cur: str) -> str | None:
    for a in soup.find_all("a"):
        txt  = a.get_text(strip=True).upper()
        aria = (a.get("aria-label") or "").upper()
        if "NEXT" in txt or "NEXT" in aria:
            h = a.get("href", "")
            if h and h not in ("#", "", "javascript:void(0)"):
                from urllib.parse import urljoin
                return urljoin(cur, h)
    # try ?page=N
    import re as _re
    from urllib.parse import urlparse, parse_qs, urlencode
    parsed = urlparse(cur)
    params = parse_qs(parsed.query)
    if "page" in params:
        try:
            pg  = int(params["page"][0]) + 1
            new = {k: v[0] for k, v in params.items()}
            new["page"] = str(pg)
            return parsed._replace(query=urlencode(new)).geturl()
        except: pass
    if not parsed.query:
        return cur + "?page=2"
    return None

def _do_refresh():
    """Actual scraping work. Always runs in a background thread."""
    global _cache, _cache_ts, _refreshing
    log.info("Refreshing card cache…")
    all_cards: list[dict] = []
    seen: set[str] = set()
    cur = BASE_URL
    visited: set[str] = set()

    while cur:
        if cur in visited:
            break
        visited.add(cur)
        try:
            soup  = _get_soup(cur)
            cards = _scrape_page(cur)
        except Exception as e:
            log.warning(f"Failed page {cur}: {e}")
            break

        new = 0
        for c in cards:
            key = f"{c['sub']}/{c['name']}"
            if key not in seen:
                seen.add(key)
                all_cards.append(c)
                new += 1

        if new == 0:
            break

        nxt = _next_page_url(soup, cur)
        if not nxt or nxt == cur or nxt in visited:
            break
        cur = nxt
        time.sleep(1.0)

    if all_cards:
        _cache    = all_cards
        _cache_ts = time.time()
        log.info(f"Cache ready — {len(_cache)} cards")
    else:
        log.warning("Cache refresh got 0 cards")

    with _refresh_lock:
        _refreshing = False

def refresh_cache(force: bool = False):
    """Non-blocking. Starts a background refresh if needed, returns immediately."""
    global _refreshing
    now = time.time()
    needs_refresh = force or (now - _cache_ts) >= CACHE_TTL

    if not needs_refresh:
        return

    with _refresh_lock:
        if _refreshing:
            return          # already running
        _refreshing = True

    t = threading.Thread(target=_do_refresh, daemon=True)
    t.start()

def get_cards() -> list[dict]:
    # Trigger background refresh if TTL expired, but always return current cache
    refresh_cache()
    return _cache

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  IMAGE HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def img_url(name: str, sub: str, size: str) -> str:
    return f"{IMG_ROOT}{sub}/{name}_{size}.png"

def _safe_clen(headers) -> int:
    try:
        return int(headers.get("content-length") or 0)
    except (ValueError, TypeError):
        return 0

def probe_url(name: str, sub: str, size: str) -> str | None:
    """Find a working image URL — try scraped sub first, then all known."""
    candidates = [sub] + [s for s in KNOWN_SUBS if s != sub]
    for s in candidates:
        url = img_url(name, s, size)
        try:
            r = http.head(url, timeout=8, allow_redirects=True)
            if r.status_code == 200:
                clen = _safe_clen(r.headers)
                if clen > 2048 or clen == 0:
                    return url
        except Exception:
            pass
        try:
            r = http.get(url, timeout=8, stream=True)
            try:
                if r.status_code == 200:
                    chunk = next(r.iter_content(4096), b"")
                    if len(chunk) > 100:
                        return url
            finally:
                r.close()
        except Exception:
            pass
    return None

def fetch_image(url: str) -> BytesIO | None:
    try:
        r = http.get(url, timeout=20, stream=True)
        r.raise_for_status()
        buf = BytesIO()
        for chunk in r.iter_content(8192):
            if chunk:
                buf.write(chunk)
        buf.seek(0)
        if buf.getbuffer().nbytes < 2048:
            return None
        return buf
    except Exception:
        return None

def get_user_size(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    return ctx.user_data.get("size", "large")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  KEYBOARDS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def card_keyboard(name: str, sub: str, cur_size: str, page: int = 0) -> InlineKeyboardMarkup:
    size_row = [
        InlineKeyboardButton(
            f"{'✅ ' if s == cur_size else ''}{SIZE_LABELS[s]}",
            callback_data=f"size|{name}|{sub}|{s}|{page}"
        )
        for s in SIZES
    ]
    action_row = [
        InlineKeyboardButton("📥 Download", callback_data=f"dl|{name}|{sub}|{cur_size}"),
        InlineKeyboardButton("🔀 Random",   callback_data=f"rand|{cur_size}"),
    ]
    upscale_row = [
        InlineKeyboardButton("🔍 Upscale 2x", callback_data=f"up|{name}|{sub}|{cur_size}|2"),
        InlineKeyboardButton("🔍 Upscale 4x", callback_data=f"up|{name}|{sub}|{cur_size}|4"),
    ]
    return InlineKeyboardMarkup([size_row, action_row, upscale_row])

def search_keyboard(results: list[dict], query: str, page: int, size: str) -> InlineKeyboardMarkup:
    rows = []
    for i, c in enumerate(results[:8]):
        rows.append([InlineKeyboardButton(
            f"{SUB_LABELS.get(c['sub'], c['sub'])}  •  {c['display']}",
            callback_data=f"pick|{c['name']}|{c['sub']}|{size}|0"
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅ Prev", callback_data=f"srch|{query}|{page-1}|{size}"))
    if len(results) > 8:
        nav.append(InlineKeyboardButton("Next ➡", callback_data=f"srch|{query}|{page+1}|{size}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)

def browse_keyboard(size: str) -> InlineKeyboardMarkup:
    rows = []
    subs = list(SUB_LABELS.keys())
    for i in range(0, len(subs), 2):
        row = []
        for sub in subs[i:i+2]:
            row.append(InlineKeyboardButton(
                SUB_LABELS[sub],
                callback_data=f"cat|{sub}|0|{size}"
            ))
        rows.append(row)
    return InlineKeyboardMarkup(rows)

def category_keyboard(cards: list[dict], sub: str, page: int, size: str) -> InlineKeyboardMarkup:
    start = page * 8
    chunk = cards[start:start+8]
    rows  = []
    for c in chunk:
        rows.append([InlineKeyboardButton(
            c["display"],
            callback_data=f"pick|{c['name']}|{c['sub']}|{size}|0"
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅ Prev", callback_data=f"cat|{sub}|{page-1}|{size}"))
    if start + 8 < len(cards):
        nav.append(InlineKeyboardButton("Next ➡", callback_data=f"cat|{sub}|{page+1}|{size}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔙 Back", callback_data=f"browse|{size}")])
    return InlineKeyboardMarkup(rows)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  UPSCALE  (local PIL Lanczos — instant, no API needed)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Pre-load FSRCNN models once at startup (tiny — 39KB and 41KB)
import os as _os
_MODEL_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "models")

def _load_sr(scale: int):
    import cv2
    sr = cv2.dnn_superres.DnnSuperResImpl_create()
    sr.readModel(_os.path.join(_MODEL_DIR, f"FSRCNN_x{scale}.pb"))
    sr.setModel("fsrcnn", scale)
    return sr

_SR_CACHE: dict = {}

def _get_sr(scale: int):
    if scale not in _SR_CACHE:
        _SR_CACHE[scale] = _load_sr(scale)
    return _SR_CACHE[scale]

MAX_UPSCALE_INPUT_DIM = 1600  # cap longest side before running FSRCNN (memory/time safety)

def upscale_image(buf: BytesIO, scale: int = 2) -> BytesIO | None:
    """AI upscale using FSRCNN neural network + enhance pass."""
    try:
        import cv2
        import numpy as np
        from PIL import Image, ImageEnhance, ImageFilter

        buf.seek(0)
        img_pil = Image.open(buf).convert("RGBA")
        w, h = img_pil.size

        # Downscale very large source images first so FSRCNN stays fast
        # and doesn't blow the memory budget on Render's free tier.
        longest = max(w, h)
        if longest > MAX_UPSCALE_INPUT_DIM:
            ratio = MAX_UPSCALE_INPUT_DIM / longest
            w, h = max(1, int(w * ratio)), max(1, int(h * ratio))
            img_pil = img_pil.resize((w, h), Image.LANCZOS)

        # Extract alpha to restore later
        alpha = img_pil.split()[3]

        # FSRCNN works on BGR numpy array
        img_rgb = img_pil.convert("RGB")
        img_np  = np.array(img_rgb)
        img_bgr = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)

        # AI upscale
        sr     = _get_sr(scale)
        up_bgr = sr.upsample(img_bgr)

        # Back to PIL RGB
        up_rgb = cv2.cvtColor(up_bgr, cv2.COLOR_BGR2RGB)
        up_pil = Image.fromarray(up_rgb)

        # Enhancement pass — sharpen + contrast + color
        up_pil = up_pil.filter(ImageFilter.UnsharpMask(radius=1.2, percent=100, threshold=2))
        up_pil = ImageEnhance.Contrast(up_pil).enhance(1.1)
        up_pil = ImageEnhance.Color(up_pil).enhance(1.08)

        # Restore upscaled alpha
        up_alpha = alpha.resize((w * scale, h * scale), Image.LANCZOS)
        up_pil.putalpha(up_alpha)

        out = BytesIO()
        up_pil.save(out, format="PNG")
        out.seek(0)
        log.info(f"FSRCNN {scale}x: {w}x{h} -> {w*scale}x{h*scale}")
        return out
    except Exception as e:
        log.warning(f"Upscale error: {e}")
        return None

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SEND CARD HELPER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def send_card(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                    card: dict, size: str, edit: bool = False):
    name = card["name"]
    sub  = card["sub"]
    url  = probe_url(name, sub, size)

    if not url:
        txt = f"⚠️ *{card['display']}* — no working image found."
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(txt, parse_mode=ParseMode.MARKDOWN)
        else:
            msg = update.message or update.callback_query.message
            await msg.reply_text(txt, parse_mode=ParseMode.MARKDOWN)
        return

    buf = fetch_image(url)
    if not buf:
        txt = "⚠️ Failed to fetch image. Try again."
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(txt)
        else:
            msg = update.message or update.callback_query.message
            await msg.reply_text(txt)
        return

    caption = (
        f"*{card['display']}*\n"
        f"{SUB_LABELS.get(sub, sub)}  •  {SIZE_LABELS[size]}\n"
        + (f"\n_{card['desc']}_" if card.get('desc') else "")
    )
    kb = card_keyboard(name, sub, size)

    try:
        if edit and update.callback_query:
            await update.callback_query.edit_message_media(
                media=InputMediaPhoto(media=buf, caption=caption, parse_mode=ParseMode.MARKDOWN),
                reply_markup=kb
            )
        else:
            msg = update.message or update.callback_query.message
            await msg.reply_photo(photo=buf, caption=caption,
                                  parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except Exception as e:
        log.warning(f"send_card error: {e}")
        msg = update.message or (update.callback_query.message if update.callback_query else None)
        if msg:
            await msg.reply_text(f"⚠️ Error sending image: {e}")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  COMMANDS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "⚡ *VALORANT Playercard Bot*\n\n"
        "*Commands:*\n"
        "🔍 `/search <name>` — search cards\n"
        "📥 `/download <name>` — download a card\n"
        "🔀 `/random` — random card\n"
        "📂 `/browse` — browse by category\n"
        "🖼 `/size` — set preferred size\n"
        "📊 `/stats` — cache info\n"
        "🔍 `/upscale` — upscale your own image\n"
        "❓ `/help` — show this menu\n\n"
        "_Tip: just send me any photo directly and I'll offer to upscale it._"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cards = get_cards()
    by_sub: dict[str, int] = {}
    for c in cards:
        by_sub[c["sub"]] = by_sub.get(c["sub"], 0) + 1
    age = int((time.time() - _cache_ts) / 60)
    lines = [f"📊 *Cache Stats* — {len(cards)} cards  _(refreshed {age}m ago)_\n"]
    for sub, count in sorted(by_sub.items(), key=lambda x: -x[1]):
        lines.append(f"{SUB_LABELS.get(sub, sub)}:  *{count}*")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_size(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cur = get_user_size(ctx)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"{'✅ ' if s == cur else ''}{SIZE_LABELS[s]}",
            callback_data=f"setsize|{s}"
        )
        for s in SIZES
    ]])
    await update.message.reply_text(
        f"🖼 *Current size:* {SIZE_LABELS[cur]}\n\nPick your preferred size:",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb
    )

async def cmd_random(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cards = get_cards()
    if not cards:
        await update.message.reply_text("⚠️ Card cache is empty. Try again in a moment.")
        return
    card = random.choice(cards)
    size = get_user_size(ctx)
    await update.message.reply_text(f"🔀 Random card: *{card['display']}*", parse_mode=ParseMode.MARKDOWN)
    await send_card(update, ctx, card, size)

async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = " ".join(ctx.args).strip()
    if not query:
        await update.message.reply_text("Usage: `/search <card name>`\nExample: `/search Astra`",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    await _do_search(update, ctx, query, page=0)

async def _do_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                     query: str, page: int):
    cards = get_cards()
    q = query.lower()
    results = [c for c in cards if q in c["name"].lower() or q in c["display"].lower()]

    msg = update.message or update.callback_query.message

    if not results:
        await msg.reply_text(f"❌ No cards found for *{query}*", parse_mode=ParseMode.MARKDOWN)
        return

    size  = get_user_size(ctx)
    start = page * 8
    chunk = results[start:start+8]

    text = (
        f"🔍 *Results for:* `{query}`\n"
        f"Found *{len(results)}* card(s)  •  Page {page+1}/{(len(results)-1)//8+1}\n\n"
        "Tap a card to preview it:"
    )
    kb = search_keyboard(chunk, query, page, size)
    await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def cmd_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = " ".join(ctx.args).strip()
    if not query:
        await update.message.reply_text("Usage: `/download <card name>`", parse_mode=ParseMode.MARKDOWN)
        return
    cards  = get_cards()
    q      = query.lower()
    matches = [c for c in cards if q in c["name"].lower() or q in c["display"].lower()]
    if not matches:
        await update.message.reply_text(f"❌ No cards found for *{query}*", parse_mode=ParseMode.MARKDOWN)
        return
    card = matches[0]
    size = get_user_size(ctx)
    url  = probe_url(card["name"], card["sub"], size)
    if not url:
        await update.message.reply_text("⚠️ Could not find a working image URL.")
        return
    buf = fetch_image(url)
    if not buf:
        await update.message.reply_text("⚠️ Failed to fetch image.")
        return
    fname = f"{card['name']}_{size}.png"
    token = register_temp_output(buf, suffix=".png")
    buf.seek(0)
    await update.message.reply_document(
        document=buf,
        filename=fname,
        caption=f"📥 *{card['display']}*  •  {SIZE_LABELS[size]}",
        parse_mode=ParseMode.MARKDOWN
    )
    await update.message.reply_text(
        "🗑 Want me to delete this image from the server now?\n"
        "_If you don't tap the button, it'll be auto-deleted from server storage in 5 minutes._",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=delete_prompt_keyboard(token)
    )

async def cmd_browse(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    size = get_user_size(ctx)
    await update.message.reply_text(
        "📂 *Browse by Category*\n\nChoose a category:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=browse_keyboard(size)
    )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  TEMP FILE STORAGE + AUTO-CLEANUP  (free-tier disk hygiene)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
import uuid

TMP_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "tmp_outputs")
_os.makedirs(TMP_DIR, exist_ok=True)
AUTO_DELETE_SECONDS = 5 * 60  # 5 minutes

_pending_deletes: dict = {}  # token -> {"path": str, "timer": threading.Timer}

def _save_temp_file(buf: BytesIO, suffix: str = ".png") -> tuple[str, str]:
    """Writes buf to TMP_DIR under a random token filename. Returns (token, path)."""
    token = uuid.uuid4().hex
    path  = _os.path.join(TMP_DIR, f"{token}{suffix}")
    buf.seek(0)
    with open(path, "wb") as f:
        f.write(buf.read())
    return token, path

def _delete_temp_file(token: str) -> bool:
    """Deletes the stored file for token, if it still exists. Cancels its timer."""
    entry = _pending_deletes.pop(token, None)
    if entry is None:
        return False
    timer = entry.get("timer")
    if timer:
        timer.cancel()
    path = entry["path"]
    try:
        if _os.path.exists(path):
            _os.remove(path)
            log.info(f"Deleted temp file {path}")
        return True
    except Exception as e:
        log.warning(f"Failed to delete temp file {path}: {e}")
        return False

def _schedule_auto_delete(token: str):
    timer = threading.Timer(AUTO_DELETE_SECONDS, _delete_temp_file, args=(token,))
    timer.daemon = True
    if token in _pending_deletes:
        _pending_deletes[token]["timer"] = timer
    timer.start()

def register_temp_output(buf: BytesIO, suffix: str = ".png") -> str:
    """Persists an in-memory output to disk, tracks it, and arms the 5-min auto-delete.
    Returns the token used to reference it (in callback_data and for manual delete)."""
    token, path = _save_temp_file(buf, suffix)
    _pending_deletes[token] = {"path": path, "timer": None}
    _schedule_auto_delete(token)
    return token

def delete_prompt_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑 Delete from server", callback_data=f"delimg|{token}")
    ]])

async def cmd_upscale(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🖼 *Upscale Your Own Image*\n\n"
        "Just send me any photo or image file — no need to type anything else.\n"
        "I'll come back with 2x and 4x upscale options.",
        parse_mode=ParseMode.MARKDOWN
    )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  USER PHOTO UPLOAD → UPSCALE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MAX_USER_IMAGE_BYTES = 20 * 1024 * 1024  # Telegram bot API download cap

def user_upscale_keyboard(file_id: str) -> InlineKeyboardMarkup:
    # file_id is a Telegram-issued token — safe to embed directly in callback_data.
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔍 Upscale 2x", callback_data=f"upu|{file_id}|2"),
        InlineKeyboardButton("🔍 Upscale 4x", callback_data=f"upu|{file_id}|4"),
    ]])

async def on_user_image(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handles a user sending a photo, or an image sent as a file/document."""
    msg = update.message
    tg_file = None
    src_name = "image.png"

    if msg.photo:
        tg_file = await msg.photo[-1].get_file()  # highest-res variant
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        if msg.document.file_size and msg.document.file_size > MAX_USER_IMAGE_BYTES:
            await msg.reply_text("⚠️ That image is too large for me to fetch (20MB limit).")
            return
        tg_file = await msg.document.get_file()
        src_name = msg.document.file_name or src_name
    else:
        return

    await msg.reply_text(
        "🖼 Got your image! Pick an upscale strength:",
        reply_markup=user_upscale_keyboard(tg_file.file_id)
    )

async def _upscale_user_file(q, ctx: ContextTypes.DEFAULT_TYPE, file_id: str, scale: int):
    await q.message.reply_text(f"⏳ Upscaling {scale}x… this takes ~10 seconds.")
    try:
        tg_file = await ctx.bot.get_file(file_id)
        raw = await tg_file.download_as_bytearray()
    except Exception as e:
        log.warning(f"User image download error: {e}")
        await q.message.reply_text("⚠️ Couldn't re-fetch that image — please send it again.")
        return

    buf = BytesIO(bytes(raw))
    out = upscale_image(buf, scale)
    if not out:
        await q.message.reply_text("⚠️ Upscale failed. Try sending the image again.")
        return

    # Persist to disk so it can be deleted on request / auto-cleaned after 5 min.
    token = register_temp_output(out, suffix=".png")

    out.seek(0)
    await q.message.reply_document(
        document=out,
        filename=f"upscaled_{scale}x.png",
        caption=f"✅ *Upscaled {scale}x*\nSharpened + contrast/color enhanced.",
        parse_mode=ParseMode.MARKDOWN
    )
    await q.message.reply_text(
        "🗑 Want me to delete this image from the server now?\n"
        "_If you don't tap the button, it'll be auto-deleted from server storage in 5 minutes._",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=delete_prompt_keyboard(token)
    )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CALLBACK HANDLER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    parts = data.split("|")
    action = parts[0]

    # ── set preferred size globally ──
    if action == "setsize":
        size = parts[1]
        ctx.user_data["size"] = size
        await q.edit_message_text(
            f"✅ Default size set to *{SIZE_LABELS[size]}*\n\nUse /browse or /search to find cards.",
            parse_mode=ParseMode.MARKDOWN
        )

    # ── size toggle on a card view ──
    elif action == "size":
        _, name, sub, size, page = parts
        ctx.user_data["size"] = size
        cards = get_cards()
        card  = next((c for c in cards if c["name"] == name and c["sub"] == sub), None)
        if not card:
            card = {"name": name, "sub": sub, "display": name, "desc": ""}
        await send_card(update, ctx, card, size, edit=True)

    # ── download file ──
    elif action == "dl":
        _, name, sub, size = parts
        cards = get_cards()
        card  = next((c for c in cards if c["name"] == name and c["sub"] == sub),
                     {"name": name, "sub": sub, "display": name, "desc": ""})
        url = probe_url(name, sub, size)
        if not url:
            await q.message.reply_text("⚠️ No working URL found.")
            return
        buf = fetch_image(url)
        if not buf:
            await q.message.reply_text("⚠️ Failed to fetch image.")
            return
        fname = f"{name}_{size}.png"
        token = register_temp_output(buf, suffix=".png")
        buf.seek(0)
        await q.message.reply_document(
            document=buf,
            filename=fname,
            caption=f"📥 *{card['display']}*  •  {SIZE_LABELS[size]}",
            parse_mode=ParseMode.MARKDOWN
        )
        await q.message.reply_text(
            "🗑 Want me to delete this image from the server now?\n"
            "_If you don't tap the button, it'll be auto-deleted from server storage in 5 minutes._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=delete_prompt_keyboard(token)
        )

    # ── random ──
    elif action == "rand":
        size  = parts[1] if len(parts) > 1 else "large"
        cards = get_cards()
        if not cards:
            await q.message.reply_text("⚠️ Cache empty.")
            return
        card = random.choice(cards)
        await send_card(update, ctx, card, size)

    # ── pick a card from search/browse list ──
    elif action == "pick":
        _, name, sub, size, page = parts
        cards = get_cards()
        card  = next((c for c in cards if c["name"] == name and c["sub"] == sub),
                     {"name": name, "sub": sub, "display": name, "desc": ""})
        await send_card(update, ctx, card, size)

    # ── search pagination ──
    elif action == "srch":
        _, query, page, size = parts
        await _do_search(update, ctx, query, int(page))

    # ── browse category ──
    elif action == "cat":
        _, sub, page, size = parts
        page  = int(page)
        cards = [c for c in get_cards() if c["sub"] == sub]
        if not cards:
            await q.edit_message_text(f"❌ No cards found in {SUB_LABELS.get(sub, sub)}")
            return
        start = page * 8
        text  = (
            f"{SUB_LABELS.get(sub, sub)}\n"
            f"*{len(cards)} cards*  •  Page {page+1}/{(len(cards)-1)//8+1}\n\n"
            "Tap a card to preview:"
        )
        await q.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=category_keyboard(cards, sub, page, size)
        )

    # ── upscale ──
    elif action == "up":
        _, name, sub, size, scale = parts
        scale = int(scale)
        cards = get_cards()
        card  = next((c for c in cards if c["name"] == name and c["sub"] == sub),
                     {"name": name, "sub": sub, "display": name, "desc": ""})
        url = probe_url(name, sub, size)
        if not url:
            await q.message.reply_text("⚠️ Could not find image to upscale.")
            return
        await q.message.reply_text(f"⏳ Upscaling {scale}x… this takes ~10 seconds.")
        buf = fetch_image(url)
        if not buf:
            await q.message.reply_text("⚠️ Failed to fetch image.")
            return
        out = upscale_image(buf, scale)
        if not out:
            await q.message.reply_text("⚠️ Upscale failed. Try again in a moment.")
            return
        token = register_temp_output(out, suffix=".png")
        fname = f"{name}_{size}_{scale}x.png"
        out.seek(0)
        await q.message.reply_document(
            document=out,
            filename=fname,
            caption=f"🔍 *{card['display']}*  •  {SIZE_LABELS[size]}  •  {scale}x upscaled",
            parse_mode=ParseMode.MARKDOWN
        )
        await q.message.reply_text(
            "🗑 Want me to delete this image from the server now?\n"
            "_If you don't tap the button, it'll be auto-deleted from server storage in 5 minutes._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=delete_prompt_keyboard(token)
        )

    # ── delete a server-stored upscaled image (user-confirmed) ──
    elif action == "delimg":
        _, token = parts
        ok = _delete_temp_file(token)
        if ok:
            await q.edit_message_text("✅ Deleted from server storage.")
        else:
            await q.edit_message_text("ℹ️ Already deleted (or auto-cleaned after 5 minutes).")

    # ── upscale a user-uploaded image ──
    elif action == "upu":
        _, file_id, scale = parts
        await _upscale_user_file(q, ctx, file_id, int(scale))

    # ── back to browse ──
    elif action == "browse":
        size = parts[1] if len(parts) > 1 else get_user_size(ctx)
        await q.edit_message_text(
            "📂 *Browse by Category*\n\nChoose a category:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=browse_keyboard(size)
        )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  FLASK PING SERVER  (UptimeRobot keepalive)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
flask_app = Flask(__name__)

@flask_app.route("/")
def ping():
    return f"⚡ KA Bot alive — {len(_cache)} cards cached", 200

@flask_app.route("/health")
def health():
    return {"status": "ok", "cards": len(_cache)}, 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MAIN
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def run_bot():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("search",   cmd_search))
    app.add_handler(CommandHandler("download", cmd_download))
    app.add_handler(CommandHandler("random",   cmd_random))
    app.add_handler(CommandHandler("browse",   cmd_browse))
    app.add_handler(CommandHandler("size",     cmd_size))
    app.add_handler(CommandHandler("stats",    cmd_stats))
    app.add_handler(CommandHandler("upscale",  cmd_upscale))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_user_image))
    app.add_handler(CallbackQueryHandler(on_callback))

    # Register command menu in Telegram chatbox
    from telegram import BotCommand
    commands = [
        BotCommand("search",   "Search cards by name"),
        BotCommand("browse",   "Browse cards by category"),
        BotCommand("random",   "Get a random playercard"),
        BotCommand("download", "Download a card as file"),
        BotCommand("size",     "Set preferred card size"),
        BotCommand("stats",    "Show cache info"),
        BotCommand("upscale",  "Upscale your own image"),
        BotCommand("help",     "Show this menu"),
    ]

    log.info("Bot starting…")
    await app.initialize()
    await app.bot.set_my_commands(commands)
    await app.start()
    await app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )
    # Keep running until interrupted
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable not set")

    # Start Flask FIRST — Render health-checks immediately on deploy
    threading.Thread(target=run_flask, daemon=True).start()
    log.info(f"Ping server running on port {PORT}")

    # Give Flask 2s to bind before anything else starts
    time.sleep(2)

    # Warm cache in background (non-blocking)
    threading.Thread(target=refresh_cache, args=(True,), daemon=True).start()

    asyncio.run(run_bot())

if __name__ == "__main__":
    main()
