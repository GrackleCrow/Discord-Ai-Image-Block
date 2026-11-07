"""
Discord AI Image Block - Discord bot that removes AI-generated images.

Checks run in order on every posted image, cheapest first; the first hit wins:

  1. C2PA Content Credentials (local, free)
     Signed provenance data that Gemini, ChatGPT, Adobe Firefly, Microsoft,
     Stability's hosted service and others embed. Flags any manifest that
     declares AI generation or a SynthID watermark - from any issuer.

  2. Generator metadata (local, free)
     - IPTC/XMP "DigitalSourceType = trainedAlgorithmicMedia" tags
     - Stable Diffusion tool fingerprints: Automatic1111 / Forge / SD.Next
       "parameters", ComfyUI workflows, InvokeAI, NovelAI, Fooocus
     - EXIF Software tags naming an AI generator

  3. OpenAI Content Provenance API (needs OPENAI_API_KEY)
     Pixel-level SynthID check for ChatGPT / OpenAI images - survives
     screenshots and metadata stripping.

  4. Sightengine AI-image classifier (optional, needs SIGHTENGINE keys)
     A machine-learning guess for images with no watermark or metadata at all
     (Midjourney, Flux, Grok, screenshots...). It can be wrong, so by default
     it only flags for mod review instead of deleting.

Reactions:  ✅ passed every check   ⚠️ classifier suspects AI (review)
            🤖 would be deleted (DRY_RUN)   ❔ a check couldn't finish
"""

import asyncio
import hashlib
import heapq
import io
import json
import logging
import logging.handlers
import os
import re
import shutil
import sqlite3
import time
import zlib
from collections import OrderedDict
from urllib.parse import urlparse, unquote

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from PIL import Image

try:
    import c2pa
except ImportError:
    c2pa = None

try:  # HEIC/HEIF support (iPhone photos); Pillow handles AVIF itself
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

VERSION = "1.1.1"  # see CHANGELOG.md

# ---------------------------------------------------------------- config ---
def env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def env_ids(name: str) -> set[int]:
    return {int(x) for x in os.getenv(name, "").split(",") if x.strip()}


TOKEN = os.environ["DISCORD_TOKEN"]
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
SIGHTENGINE_USER = os.getenv("SIGHTENGINE_API_USER", "").strip()
SIGHTENGINE_SECRET = os.getenv("SIGHTENGINE_API_SECRET", "").strip()
SIGHTENGINE_THRESHOLD = float(os.getenv("SIGHTENGINE_THRESHOLD", "0.90"))
CLASSIFIER_ACTION = os.getenv("CLASSIFIER_ACTION", "review").strip().lower()  # review | delete
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0") or 0)
EXEMPT_ROLE_IDS = env_ids("EXEMPT_ROLE_IDS")
WATCH_CHANNEL_IDS = env_ids("WATCH_CHANNEL_IDS")
NOTIFY_USER = env_bool("NOTIFY_USER", True)
DRY_RUN = env_bool("DRY_RUN", False)
REACT_CLEAN = env_bool("REACT_CLEAN", True)
METADATA_CHECK = env_bool("METADATA_CHECK", True)
DB_PATH = os.getenv("DB_PATH", "/app/data/synthid.db")
QUARANTINE_DIR = os.getenv("QUARANTINE_DIR", os.path.join(os.path.dirname(DB_PATH) or ".", "quarantine"))
QUARANTINE_DAYS = int(os.getenv("QUARANTINE_DAYS", "30"))  # how long removed posts can be restored
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "500"))  # Discord's largest upload (Nitro) is 500 MB
MAX_BYTES = MAX_FILE_MB * 1024 * 1024
API_MAX_BYTES = 45 * 1024 * 1024  # bigger images are shrunk before the online checks (OpenAI limit: 50 MB)
# File that every removal, review, and restore is written to. Blank = off.
REMOVAL_LOG_FILE = os.getenv("REMOVAL_LOG_FILE", os.path.join(os.path.dirname(DB_PATH) or ".", "removals.log"))
# Online checks are shared: real posts always go ahead of /ai-check requests
PRIORITY_POST, PRIORITY_CHECK = 0, 1

REACT_OK, REACT_REVIEW, REACT_DRY, REACT_UNKNOWN = "✅", "⚠️", "🤖", "❔"

IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif",
               "image/avif", "image/heic", "image/heif"}
# Formats the online APIs may not accept - converted to PNG just for those checks
CONVERT_FOR_API = {"image/avif", "image/heic", "image/heif"}
SCAN_LINKS = env_bool("SCAN_LINKS", True)          # images posted as links (Imgur, direct URLs...)
SCAN_GIF_LINKS = env_bool("SCAN_GIF_LINKS", False)  # Tenor/Giphy reaction GIFs (uses API quota)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("discord-ai-image-block")

# Removal log: one line per removal / review / restore, rotated at 5 MB (keeps 5 old files)
removal_log = logging.getLogger("discord-ai-image-block.removals")
removal_log.propagate = False
removal_log.setLevel(logging.INFO)
if REMOVAL_LOG_FILE:
    try:
        os.makedirs(os.path.dirname(REMOVAL_LOG_FILE) or ".", exist_ok=True)
        _fh = logging.handlers.RotatingFileHandler(REMOVAL_LOG_FILE, maxBytes=5 * 1024 * 1024,
                                                   backupCount=5, encoding="utf-8")
        _fh.setFormatter(logging.Formatter("%(asctime)s | %(message)s", "%Y-%m-%d %H:%M:%S"))
        removal_log.addHandler(_fh)
    except OSError as e:
        log.error("Can't write the removal log at %s: %s", REMOVAL_LOG_FILE, e)


def log_event(event: str, guild, channel, user_name, user_id, det_id=None, file=None, reason=None, by=None):
    parts = [event if det_id is None else f"{event} #{det_id}",
             f"server={getattr(guild, 'name', guild)} ({getattr(guild, 'id', '?')})",
             f"channel=#{getattr(channel, 'name', channel)} ({getattr(channel, 'id', '?')})",
             f"user={user_name} ({user_id})"]
    if file:
        parts.append(f"file={file}")
    if reason:
        parts.append(f"reason={reason}")
    if by:
        parts.append(f"by={by} ({getattr(by, 'id', '?')})")
    removal_log.info(" | ".join(parts))

# Verdicts
FLAG, SUSPECT, CLEAN, UNKNOWN = "flag", "suspect", "clean", "unknown"


# -------------------------------------------- fast metadata extraction ---
# Only the metadata parts of the file are read - pixel data is skipped
# entirely, so a 20 MB photo costs about the same as a 20 KB one.
class Meta:
    __slots__ = ("region", "text", "exif")

    def __init__(self, region: bytes, text: dict, exif):
        self.region = region  # concatenated non-pixel bytes (EXIF, XMP, C2PA, text chunks)
        self.text = text      # lower-cased key -> str (PNG text chunks / Pillow info)
        self.exif = exif      # PIL Image.Exif or None


ZTXT_LIMIT = 8 * 1024 * 1024  # cap decompressed text chunks (zip-bomb guard)


def _inflate(blob: bytes) -> bytes:
    try:
        return zlib.decompressobj().decompress(blob, ZTXT_LIMIT)
    except zlib.error:
        return b""


def _png_meta(data: bytes) -> Meta:
    parts, text, exif_raw = [], {}, None
    pos, n = 8, len(data)
    while pos + 8 <= n:
        length = int.from_bytes(data[pos:pos + 4], "big")
        ctype = data[pos + 4:pos + 8]
        body_end = pos + 8 + length
        if ctype == b"IEND" or body_end > n:
            break
        if ctype not in (b"IDAT", b"fdAT"):  # skip pixel data (and APNG frames)
            body = data[pos + 8:body_end]
            parts.append(body)
            try:
                if ctype == b"tEXt":
                    k, _, v = body.partition(b"\0")
                    text[k.decode("latin-1").lower()] = v.decode("latin-1")
                elif ctype == b"zTXt":
                    k, _, rest = body.partition(b"\0")
                    v = _inflate(rest[1:])
                    text[k.decode("latin-1").lower()] = v.decode("latin-1")
                    parts.append(v)
                elif ctype == b"iTXt":
                    k, _, rest = body.partition(b"\0")
                    compressed, rest = rest[0], rest[2:]
                    _, _, rest = rest.partition(b"\0")      # language tag
                    _, _, v = rest.partition(b"\0")         # translated keyword
                    if compressed:
                        v = _inflate(v)
                        parts.append(v)
                    text[k.decode("latin-1").lower()] = v.decode("utf-8", "replace")
                elif ctype == b"eXIf":
                    exif_raw = body
            except Exception:
                pass
        pos = body_end + 4  # skip CRC
    exif = None
    if exif_raw:
        try:
            exif = Image.Exif()
            exif.load(exif_raw)
        except Exception:
            exif = None
    return Meta(b"".join(parts), text, exif)


def _jpeg_region(data: bytes) -> bytes:
    parts, pos, n = [], 2, len(data)
    while pos + 4 <= n and data[pos] == 0xFF:
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker == 0xDA:  # start of scan - pixel data follows
            break
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        length = int.from_bytes(data[pos + 2:pos + 4], "big")
        parts.append(data[pos + 4:pos + 2 + length])
        pos += 2 + length
    return b"".join(parts)


def _webp_region(data: bytes) -> bytes:
    parts, pos, n = [], 12, len(data)
    while pos + 8 <= n:
        ctype = data[pos:pos + 4]
        size = int.from_bytes(data[pos + 4:pos + 8], "little")
        if ctype not in (b"VP8 ", b"VP8L", b"ALPH", b"ANMF"):
            parts.append(data[pos + 8:pos + 8 + size])
        pos += 8 + size + (size & 1)
    return b"".join(parts)


def extract_meta(data: bytes, mime: str) -> Meta:
    if mime == "image/png" and data[:8] == b"\x89PNG\r\n\x1a\n":
        return _png_meta(data)
    if mime == "image/jpeg" and data[:2] == b"\xff\xd8":
        region = _jpeg_region(data)
    elif mime == "image/webp" and data[:4] == b"RIFF":
        region = _webp_region(data)
    else:
        region = data
    text, exif = {}, None
    try:  # Pillow only parses headers for these formats - no pixel decoding
        img = Image.open(io.BytesIO(data))
        text = {str(k).lower(): v for k, v in img.info.items()}
        exif = img.getexif()
    except Exception:
        pass
    return Meta(region, text, exif)


def _text(v) -> str:
    if isinstance(v, bytes):
        # UTF-16 text (A1111's EXIF UserComment) decodes as ASCII + NULs
        return v.decode("utf-8", errors="ignore").replace("\x00", "")
    return str(v)


# ------------------------------------------------------- 1. C2PA check ---
# Each check returns a Hit: (kind, log reason, detail for the user message)
Hit = tuple[str, str, str]


def check_c2pa(data: bytes, mime: str, meta: Meta) -> Hit | None:
    # Every embedded C2PA manifest lives in a JUMBF box; no box = nothing to parse.
    if c2pa is None or b"jumb" not in meta.region:
        return None
    try:
        with c2pa.Reader(mime, io.BytesIO(data)) as reader:
            report = json.loads(reader.json())
    except Exception:
        return None

    state = str(report.get("validation_state", "unknown"))
    for manifest in report.get("manifests", {}).values():
        blob = json.dumps(manifest).lower()
        issuer = manifest.get("signature_info", {}).get("issuer") or "unknown issuer"
        vendor = "gemini" if "google" in issuer.lower() else "chatgpt" if "openai" in issuer.lower() else None
        if "synthid" in blob:
            return (vendor or "synthid", f"C2PA credentials report a SynthID watermark ({issuer}, {state})", issuer)
        if "trainedalgorithmicmedia" in blob:
            return (vendor or "c2pa", f"C2PA credentials declare AI generation ({issuer}, {state})", issuer)
    return None


# ------------------------------------------------ 2. Generator metadata ---
# Generator names, matched ONLY against "Software" fields and only as whole words,
# so a photo described as "imagen" (Spanish for image) or "fireflies" isn't flagged.
AI_SOFTWARE = {
    "midjourney": ("generator", "Midjourney"),
    "novelai": ("generator", "NovelAI"),
    "stable diffusion": ("stable_diffusion", "Stable Diffusion"),
    "comfyui": ("stable_diffusion", "ComfyUI"),
    "automatic1111": ("stable_diffusion", "Automatic1111"),
    "invokeai": ("stable_diffusion", "InvokeAI"),
    "fooocus": ("stable_diffusion", "Fooocus"),
    "dall-e": ("chatgpt", "DALL·E"),
    "dall·e": ("chatgpt", "DALL·E"),
    "gpt-image": ("chatgpt", "gpt-image"),
    "adobe firefly": ("generator", "Adobe Firefly"),
    "leonardo.ai": ("generator", "Leonardo.Ai"),
    "ideogram": ("generator", "Ideogram"),
    "nightcafe": ("generator", "NightCafe"),
    "dreamstudio": ("stable_diffusion", "DreamStudio"),
}
AI_SOFTWARE_RE = re.compile(
    r"(?<![a-z0-9])(" + "|".join(re.escape(k) for k in AI_SOFTWARE) + r")(?![a-z0-9])")
# Stable Diffusion settings text: "Steps: 20, Sampler: Euler a, CFG scale: 7"
SD_PARAMS_RE = re.compile(r"\bsteps:\s*\d+.*\b(sampler|cfg scale):", re.S)
# "rainedAlgorithmicMedia" matches both trainedAlgorithmicMedia and
# compositeWithTrainedAlgorithmicMedia (AI-edited, e.g. generative fill)
RAW_MARKERS = re.compile(rb"rainedAlgorithmicMedia|[Ss]ynth[Ii][Dd]")


def check_metadata(meta: Meta) -> Hit | None:
    m = RAW_MARKERS.search(meta.region)
    if m:
        if m.group().endswith(b"Media"):
            return ("iptc", "IPTC metadata marks it AI-generated (trainedAlgorithmicMedia)", "")
        return ("synthid", "Embedded metadata mentions a SynthID watermark", "")

    info, exif, sd = meta.text, meta.exif, "stable_diffusion"
    params = _text(info.get("parameters", ""))
    if SD_PARAMS_RE.search(params.lower()):
        return (sd, "Stable Diffusion generation parameters (Automatic1111 / Forge / SD.Next)",
                "Automatic1111 / Forge")
    for key in ("prompt", "workflow"):
        val = _text(info.get(key, ""))
        if '"class_type"' in val or '"nodes"' in val:
            return (sd, "ComfyUI workflow embedded in the image", "ComfyUI")
    if any(k in info for k in ("invokeai_metadata", "invokeai_graph", "sd-metadata", "dream")):
        return (sd, "InvokeAI generation metadata", "InvokeAI")
    if "fooocus_scheme" in info:
        return (sd, "Fooocus generation metadata", "Fooocus")
    if _text(info.get("software", "")).strip().lower() == "novelai":
        return ("generator", "NovelAI generation metadata", "NovelAI")

    # Software fields: PNG "Software" text and EXIF Software (0x0131)
    software = [_text(info.get("software", ""))]
    # Free-text fields: EXIF ImageDescription (0x010E) and UserComment (0x9286)
    free_text = []
    if exif is not None:
        software.append(_text(exif.get(0x0131, "")))
        free_text.append(_text(exif.get(0x010E, "")))
        try:
            uc = exif.get_ifd(0x8769).get(0x9286)
            if uc:
                free_text.append(_text(uc[8:] if isinstance(uc, bytes) else uc))
        except Exception:
            pass
    for f in software + free_text:  # SD settings are distinctive enough to trust anywhere
        if SD_PARAMS_RE.search(f.lower()):
            return (sd, "Stable Diffusion generation parameters in EXIF", "Stable Diffusion")
    for f in software:              # tool names only count in Software fields
        m = AI_SOFTWARE_RE.search(f.lower())
        if m:
            kind, display = AI_SOFTWARE[m.group(1)]
            return (kind, f"Software field names an AI generator ({display})", display)
    return None


# ------------------------------------------------- 3. OpenAI SynthID API ---
class PriorityLock:
    """One request at a time; when several are waiting, the lowest priority number goes next
    (first come, first served within the same priority)."""

    def __init__(self):
        self._locked = False
        self._waiters: list = []
        self._n = 0

    async def acquire(self, priority: int):
        if not self._locked and not self._waiters:
            self._locked = True
            return
        fut = asyncio.get_running_loop().create_future()
        self._n += 1
        heapq.heappush(self._waiters, (priority, self._n, fut))
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():  # lock was handed over just as we were cancelled
                self.release()
            raise

    def release(self):
        while self._waiters:
            _, _, fut = heapq.heappop(self._waiters)
            if not fut.done():
                fut.set_result(True)  # hand the lock straight to the next waiter
                return
        self._locked = False


class RateLimitedClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.lock = PriorityLock()
        self.blocked_until = 0.0

    async def post(self, url: str, form_factory, headers=None, priority: int = PRIORITY_POST) -> dict | None:
        """POST with retry on 429/5xx. Returns JSON body, or None on failure."""
        await self.lock.acquire(priority)
        try:
            for attempt in range(4):
                wait = self.blocked_until - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    async with self.session.post(url, data=form_factory(), headers=headers) as r:
                        if r.status == 429 or r.status >= 500:
                            retry = float(r.headers.get("Retry-After", 2 ** (attempt + 1)))
                            self.blocked_until = time.monotonic() + retry
                            log.warning("%s -> %s, retrying in %.0fs", url, r.status, retry)
                            continue
                        if r.status != 200:
                            log.warning("%s -> %s: %s", url, r.status, (await r.text())[:300])
                            return None
                        return await r.json()
                except aiohttp.ClientError as e:
                    log.warning("%s request failed: %s", url, e)
                    await asyncio.sleep(2 ** attempt)
            log.error("%s gave up after retries", url)
            return None
        finally:
            self.lock.release()


class OpenAIChecker(RateLimitedClient):
    URL = "https://api.openai.com/v1/content_provenance_checks"

    def __init__(self, session, api_key):
        super().__init__(session)
        self.headers = {"Authorization": f"Bearer {api_key}"}

    async def check(self, data, filename, mime, priority=PRIORITY_POST) -> tuple[str, Hit | None]:
        def form():
            f = aiohttp.FormData()
            f.add_field("file", data, filename=filename, content_type=mime)
            return f

        body = await self.post(self.URL, form, self.headers, priority)
        if body is None:
            return UNKNOWN, None
        for res in body.get("results", []):
            if res.get("outcome") != "detected":
                continue
            model = res.get("model") or "OpenAI"
            if res.get("type") == "synthid":
                return FLAG, ("chatgpt", f"SynthID watermark detected in pixels ({model})", model)
            if res.get("type") == "c2pa":
                return FLAG, ("chatgpt", f"OpenAI C2PA credentials detected ({model})", model)
        return CLEAN, None


# ------------------------------------------- 4. Sightengine classifier ---
class SightengineChecker(RateLimitedClient):
    URL = "https://api.sightengine.com/1.0/check.json"

    def __init__(self, session, user, secret):
        super().__init__(session)
        self.user, self.secret = user, secret

    async def check(self, data, filename, mime, priority=PRIORITY_POST) -> tuple[str, Hit | None]:
        def form():
            f = aiohttp.FormData()
            f.add_field("media", data, filename=filename, content_type=mime)
            f.add_field("models", "genai")
            f.add_field("api_user", self.user)
            f.add_field("api_secret", self.secret)
            return f

        body = await self.post(self.URL, form, priority=priority)
        if not body or body.get("status") != "success":
            log.warning("Sightengine error: %s", (body or {}).get("error"))
            return UNKNOWN, None
        score = float(body.get("type", {}).get("ai_generated", 0))
        log.info("Sightengine score %.3f for %s", score, filename)
        if score >= SIGHTENGINE_THRESHOLD:
            return SUSPECT, ("classifier", f"AI-image classifier is {score:.0%} confident it's AI-generated",
                             f"{score:.0%}")
        return CLEAN, None


# ------------------------------------------------------------- tracking ---
# action: removed = deleted, test = flagged while DRY_RUN, review = ⚠️ classifier
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
db = sqlite3.connect(DB_PATH)
db.execute("PRAGMA journal_mode=WAL")      # faster writes, no reader/writer blocking
db.execute("PRAGMA synchronous=NORMAL")
db.execute(
    """CREATE TABLE IF NOT EXISTS detections (
        id INTEGER PRIMARY KEY,
        guild_id INTEGER, user_id INTEGER, user_name TEXT,
        channel_id INTEGER, filename TEXT, reason TEXT, action TEXT,
        ts INTEGER)"""
)
db.execute("CREATE INDEX IF NOT EXISTS idx_guild_user ON detections(guild_id, user_id)")
# Columns added in later versions (older databases get them on startup)
_cols = {row[1] for row in db.execute("PRAGMA table_info(detections)")}
for _col, _type in (("message_text", "TEXT"), ("saved_dir", "TEXT"),
                    ("restored_by", "INTEGER"), ("restored_ts", "INTEGER"), ("restore_reason", "TEXT"),
                    ("author_name", "TEXT"), ("avatar_url", "TEXT"), ("posted_ts", "INTEGER"),
                    ("reply_url", "TEXT")):
    if _col not in _cols:
        db.execute(f"ALTER TABLE detections ADD COLUMN {_col} {_type}")
# Images a mod restored = known false positives; the exact same file is let through next time
db.execute(
    """CREATE TABLE IF NOT EXISTS allowlist (
        guild_id INTEGER, sha256 TEXT, added_by INTEGER, ts INTEGER,
        PRIMARY KEY (guild_id, sha256))"""
)
db.execute("CREATE TABLE IF NOT EXISTS settings (guild_id INTEGER PRIMARY KEY, log_channel_id INTEGER)")
if "size" not in {row[1] for row in db.execute("PRAGMA table_info(allowlist)")}:
    db.execute("ALTER TABLE allowlist ADD COLUMN size INTEGER")
db.commit()

# Allowlist kept in memory; a file is only hashed if its exact size matches an
# allowlisted file, so normal posts never pay for hashing.
ALLOW: dict[int, set[str]] = {}
ALLOW_SIZES: dict[int, set[int | None]] = {}
for _g, _h, _sz in db.execute("SELECT guild_id, sha256, size FROM allowlist"):
    ALLOW.setdefault(_g, set()).add(_h)
    ALLOW_SIZES.setdefault(_g, set()).add(_sz)


def allow_file(guild_id: int, data: bytes, mod_id: int):
    digest = hashlib.sha256(data).hexdigest()
    db.execute("INSERT OR IGNORE INTO allowlist (guild_id, sha256, added_by, ts, size) VALUES (?,?,?,?,?)",
               (guild_id, digest, mod_id, int(time.time()), len(data)))
    ALLOW.setdefault(guild_id, set()).add(digest)
    ALLOW_SIZES.setdefault(guild_id, set()).add(len(data))


def reply_url(message: discord.Message) -> str | None:
    ref = getattr(message, "reference", None)
    if not ref or not ref.message_id or getattr(ref, "type", None) == discord.MessageReferenceType.forward:
        return None
    return f"https://discord.com/channels/{ref.guild_id or message.guild.id}/{ref.channel_id}/{ref.message_id}"


def record(message: discord.Message, att, reason: str, action: str) -> int | None:
    author = message.author
    try:
        avatar = author.display_avatar.url
    except Exception:
        avatar = None
    created = getattr(message, "created_at", None)
    try:
        cur = db.execute(
            "INSERT INTO detections (guild_id, user_id, user_name, channel_id, filename, reason, action, ts,"
            " message_text, author_name, avatar_url, posted_ts, reply_url) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (message.guild.id, author.id, str(author), message.channel.id,
             att.filename, reason, action, int(time.time()), full_text(message),
             getattr(author, "display_name", None) or str(author), avatar,
             int(created.timestamp()) if created else int(time.time()), reply_url(message)),
        )
        db.commit()
        return cur.lastrowid
    except Exception:
        log.exception("Couldn't record detection")
        return None


def is_allowlisted(guild_id: int, data: bytes) -> bool:
    sizes = ALLOW_SIZES.get(guild_id)
    if not sizes or (len(data) not in sizes and None not in sizes):
        return False
    return hashlib.sha256(data).hexdigest() in ALLOW[guild_id]


# ------------------------------------------------------------------ bot ---
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
http: aiohttp.ClientSession | None = None
openai_checker: OpenAIChecker | None = None
sightengine_checker: SightengineChecker | None = None


@client.event
async def on_ready():
    global http, openai_checker, sightengine_checker
    if http is None:
        http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120),
            connector=aiohttp.TCPConnector(limit=20, ttl_dns_cache=600, keepalive_timeout=120),
        )
        if OPENAI_API_KEY:
            openai_checker = OpenAIChecker(http, OPENAI_API_KEY)
        if SIGHTENGINE_USER and SIGHTENGINE_SECRET:
            sightengine_checker = SightengineChecker(http, SIGHTENGINE_USER, SIGHTENGINE_SECRET)
        # Sync per server so the slash commands show up instantly
        for guild in client.guilds:
            try:
                tree.copy_global_to(guild=guild)
                await tree.sync(guild=guild)
            except discord.HTTPException as e:
                log.warning("Couldn't sync commands to %s: %s", guild, e)
        if not purge_quarantine.is_running():
            purge_quarantine.start()
    onoff = lambda x: "on" if x else "off"
    log.info(
        "Discord AI Image Block v%s | Logged in as %s | C2PA: %s | metadata: %s | OpenAI SynthID: %s | classifier: %s (%s) | dry run: %s",
        VERSION, client.user, onoff(c2pa), onoff(METADATA_CHECK), onoff(openai_checker),
        onoff(sightengine_checker), CLASSIFIER_ACTION, DRY_RUN,
    )


def is_exempt(message: discord.Message) -> bool:
    if message.author.bot or message.guild is None:
        return True
    if WATCH_CHANNEL_IDS and message.channel.id not in WATCH_CHANNEL_IDS:
        return True
    return any(r.id in EXEMPT_ROLE_IDS for r in getattr(message.author, "roles", []))


def mime_of(att) -> str:
    return (att.content_type or "").split(";")[0].lower()


def sniff_mime(data: bytes) -> str:
    """Identify the image format from its first bytes (link servers often send no type)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"):
            return "image/heic"
    return ""


DISCORD_MEDIA_HOSTS = ("discordapp.net", "discordapp.com", "discord.com")


class LinkImage:
    """An image posted as a link. Downloaded through Discord's own media proxy only,
    so the bot never connects to arbitrary sites (or anything on your home network)."""

    def __init__(self, url: str, original: str):
        self.url = url
        self.content_type = ""
        self.size = 0
        host = urlparse(original).hostname or "link"
        name = unquote(urlparse(original).path.rsplit("/", 1)[-1])[:60] or "image"
        self.filename = f"{host}/{name}"

    async def read(self, use_cached=False) -> bytes:
        u = urlparse(self.url)
        if u.scheme != "https" or not (u.hostname or "").endswith(DISCORD_MEDIA_HOSTS):
            raise ValueError(f"refusing to fetch non-Discord URL {self.url}")
        async with http.get(self.url) as r:
            r.raise_for_status()
            chunks, total = [], 0
            async for chunk in r.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > MAX_BYTES:
                    raise ValueError("linked image is too large")
                chunks.append(chunk)
        data = b"".join(chunks)
        header = (r.headers.get("Content-Type") or "").split(";")[0].lower()
        self.content_type = header if header in IMAGE_TYPES else sniff_mime(data)
        self.size = len(data)
        if self.content_type not in IMAGE_TYPES:
            raise ValueError(f"link isn't a supported image ({header or 'unknown type'})")
        return data


def link_images(embeds) -> list[LinkImage]:
    """Only DIRECT image links (Discord shows them as "image" embeds). News articles,
    social posts, and other page previews are skipped - the picture belongs to the site."""
    out = []
    for e in embeds:
        if e.type == "image" or (e.type == "gifv" and SCAN_GIF_LINKS):
            media = e.thumbnail if e.thumbnail and e.thumbnail.proxy_url else None
            if media:
                out.append(LinkImage(media.proxy_url, media.url or e.url or media.proxy_url))
    return out


def source_key(src) -> str:
    return f"att:{src.id}" if isinstance(src, discord.Attachment) else f"url:{src.url}"


def all_attachments(message: discord.Message) -> list[discord.Attachment]:
    """The message's own attachments plus those of any forwarded message inside it."""
    atts = list(message.attachments)
    for snap in getattr(message, "message_snapshots", None) or []:
        atts += list(snap.attachments)
    return atts


def collect_sources(message: discord.Message) -> list:
    """Everything in a post that should be checked: attachments, forwarded attachments, image links."""
    sources = [a for a in all_attachments(message) if mime_of(a) in IMAGE_TYPES and a.size <= MAX_BYTES]
    if SCAN_LINKS:
        embeds = list(message.embeds)
        for snap in getattr(message, "message_snapshots", None) or []:
            embeds += list(snap.embeds)
        sources += link_images(embeds)
    seen, unique = set(), []
    for src in sources:
        if (k := source_key(src)) not in seen:
            seen.add(k)
            unique.append(src)
    return unique


def full_text(message: discord.Message) -> str:
    parts = [message.content or ""]
    for snap in getattr(message, "message_snapshots", None) or []:
        if snap.content:
            parts.append("(forwarded) " + snap.content)
    return "\n".join(p for p in parts if p)


def shrink_for_api(data: bytes, limit: int = API_MAX_BYTES) -> bytes:
    """Re-encode a very large image as a high-quality JPEG (downscaling if needed) so it fits
    the online checks' upload limit. Local checks always see the original file."""
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGB")
        while True:
            out = io.BytesIO()
            im.save(out, "JPEG", quality=92)
            if out.tell() <= limit or min(im.size) < 512:
                return out.getvalue()
            im = im.resize((int(im.width * 0.75), int(im.height * 0.75)), Image.LANCZOS)


def to_png(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as im:
        out = io.BytesIO()
        im.convert("RGBA" if "A" in im.getbands() else "RGB").save(out, "PNG")
        return out.getvalue()


def local_scan(data: bytes, mime: str, guild_id: int) -> tuple[str, Hit | None] | None:
    """Free offline checks (runs in a worker thread). None = passed, needs the API checks."""
    if is_allowlisted(guild_id, data):
        return CLEAN, None
    meta = extract_meta(data, mime)
    hit = check_c2pa(data, mime, meta)
    if hit:
        return FLAG, hit
    if METADATA_CHECK:
        hit = check_metadata(meta)
        if hit:
            return FLAG, hit
    return None


def is_removal(verdict: str, hit: Hit | None) -> bool:
    return bool(hit) and (verdict == FLAG or (verdict == SUSPECT and CLASSIFIER_ACTION == "delete"))


async def scan_image(data: bytes, att, guild_id: int,
                     priority: int = PRIORITY_POST) -> tuple[str, Hit | None]:
    """Checks in order: free local checks -> Sightengine classifier -> ChatGPT (OpenAI) last.
    The first check that fails ends the scan; later checks never run."""
    mime = mime_of(att) if mime_of(att) in IMAGE_TYPES else sniff_mime(data)
    res = await asyncio.to_thread(local_scan, data, mime, guild_id)
    if res:
        return res

    api_data, api_mime, api_name = data, mime, att.filename.replace("/", "_")
    if mime in CONVERT_FOR_API and (sightengine_checker or openai_checker):
        try:
            api_data = await asyncio.to_thread(to_png, data)
            api_mime, api_name = "image/png", api_name.rsplit(".", 1)[0] + ".png"
        except Exception:
            log.warning("Couldn't convert %s to PNG for the online checks", att.filename)
    if len(api_data) > API_MAX_BYTES and (sightengine_checker or openai_checker):
        try:
            api_data = await asyncio.to_thread(shrink_for_api, data, API_MAX_BYTES)
            api_mime, api_name = "image/jpeg", api_name.rsplit(".", 1)[0] + ".jpg"
            log.info("Shrank %s to %.1f MB for the online checks", att.filename, len(api_data) / 1e6)
        except Exception:
            log.warning("%s is too large for the online checks and couldn't be shrunk", att.filename)
            return UNKNOWN, None

    suspect, unknown = None, False
    for checker in (sightengine_checker, openai_checker):  # ChatGPT check runs last
        if not checker:
            continue
        verdict, hit = await checker.check(api_data, api_name, api_mime, priority)
        if verdict == FLAG:
            return FLAG, hit
        if verdict == SUSPECT:
            if CLASSIFIER_ACTION == "delete":
                return SUSPECT, hit
            # Review mode doesn't remove anything, so still let the ChatGPT check
            # confirm it - a confirmed watermark gets deleted instead of just ⚠️.
            suspect = hit
        elif verdict == UNKNOWN:
            unknown = True
    if suspect:
        return SUSPECT, suspect
    return (UNKNOWN if unknown else CLEAN), None


# ---------------------------------------------- removal notice messages ---
# Placeholders: {user} = mention, {detail} = tool / issuer / score.
# Override any of these in .env, e.g. MSG_GEMINI=...
MESSAGES = {
    "gemini": "{user} your image was removed — it was made with **Google Gemini** "
              "(Google's AI watermark was found in it).",
    "chatgpt": "{user} your image was removed — it was made with **ChatGPT** "
               "(OpenAI's AI watermark was found in it).",
    "synthid": "{user} your image was removed — it carries a **SynthID** AI watermark.",
    "c2pa": "{user} your image was removed — its Content Credentials say it was **AI-generated** "
            "(signed by {detail}).",
    "stable_diffusion": "{user} your image was removed — it has **Stable Diffusion** generation "
                        "settings saved in it ({detail}).",
    "generator": "{user} your image was removed — its metadata says it was made with **{detail}**.",
    "iptc": "{user} your image was removed — its metadata is tagged as **AI-generated**.",
    "classifier": "{user} your image was removed — our AI-image detector is **{detail}** sure "
                  "it's AI-generated. If that's wrong, let a mod know.",
}
MESSAGE_FOOTER = os.getenv("MSG_FOOTER", " AI images aren't allowed here.")
for _kind in MESSAGES:
    if os.getenv(f"MSG_{_kind.upper()}"):
        MESSAGES[_kind] = os.environ[f"MSG_{_kind.upper()}"]


def removal_notice(message: discord.Message, hit: Hit) -> str:
    kind, _, detail = hit
    template = MESSAGES.get(kind, "{user} your image was removed — it looks AI-generated.")
    try:
        text = template.format(user=message.author.mention, detail=detail or "unknown")
    except (KeyError, IndexError, ValueError):  # bad placeholder in a custom message
        text = template
    return text + MESSAGE_FOOTER


async def react(message: discord.Message, emoji: str):
    try:
        await message.add_reaction(emoji)
    except discord.HTTPException:
        pass


def log_channel_id(guild_id: int) -> int:
    row = db.execute("SELECT log_channel_id FROM settings WHERE guild_id=?", (guild_id,)).fetchone()
    return row[0] if row and row[0] is not None else LOG_CHANNEL_ID


async def log_to_channel(guild_id: int, text: str, view: discord.ui.View | None = None):
    cid = log_channel_id(guild_id)
    if cid and (ch := client.get_channel(cid)):
        try:
            kwargs = {"view": view} if view else {}
            await ch.send(text, allowed_mentions=discord.AllowedMentions.none(), **kwargs)
        except discord.HTTPException:
            pass


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:100] or "file"


async def quarantine(message: discord.Message, det_id: int, cache: dict[int, bytes],
                     links: list | None = None) -> str | None:
    """Save every attachment of the message (including forwarded ones) so a mod can restore it.
    Must run BEFORE deleting. Images already downloaded for scanning are reused.
    Linked images that were checked are saved as "L.." files: not reposted on restore
    (the link in the text brings them back), but used to remember false positives."""
    folder = os.path.join(QUARANTINE_DIR, str(det_id))
    atts = all_attachments(message)
    link_files = [(l, cache[id(l)]) for l in (links or []) if id(l) in cache]

    async def get(a):
        return cache[id(a)] if id(a) in cache else await a.read(use_cached=False)

    try:
        datas = await asyncio.gather(*(get(a) for a in atts))

        def write():
            os.makedirs(folder, exist_ok=True)
            for i, (a, data) in enumerate(zip(atts, datas)):
                with open(os.path.join(folder, f"{i:02d}_{safe_name(a.filename)}"), "wb") as f:
                    f.write(data)
            for i, (l, data) in enumerate(link_files):
                with open(os.path.join(folder, f"L{i:02d}_{safe_name(l.filename)}"), "wb") as f:
                    f.write(data)

        await asyncio.to_thread(write)
        db.execute("UPDATE detections SET saved_dir=? WHERE id=?", (folder, det_id))
        db.commit()
        return folder
    except Exception:
        log.exception("Couldn't save a restorable copy of detection #%s", det_id)
        shutil.rmtree(folder, ignore_errors=True)
        return None


# Posts already being removed. The same post can be scanned twice at once (e.g. its
# attachment, then a link preview Discord adds a moment later) - only act on it once.
_handled: "OrderedDict[int, None]" = OrderedDict()


def claim(message_id: int) -> bool:
    if message_id in _handled:
        return False
    _handled[message_id] = None
    if len(_handled) > 5000:
        _handled.popitem(last=False)
    return True


def forget_detection(det_id: int | None, folder: str | None):
    """Undo a record + saved copy when the post couldn't actually be removed."""
    if folder:
        shutil.rmtree(folder, ignore_errors=True)
    if det_id:
        db.execute("DELETE FROM detections WHERE id=?", (det_id,))
        db.commit()


async def remove(message: discord.Message, att, hit: Hit, cache: dict[int, bytes], links: list | None = None):
    reason = hit[1]
    if not claim(message.id):
        log.info("Message %s is already being handled - skipping duplicate removal", message.id)
        return
    if DRY_RUN:
        log.info("Would post: %s", removal_notice(message, hit))
        det = record(message, att, reason, "test")
        log_event("TEST (would remove)", message.guild, message.channel, str(message.author), message.author.id, det, att.filename, reason)
        await react(message, REACT_DRY)
        return
    det_id = record(message, att, reason, "removed")
    saved = await quarantine(message, det_id, cache, links) if det_id else None
    try:
        await message.delete()
    except discord.Forbidden:
        # The post is still up, so it mustn't count against the member or be restorable
        forget_detection(det_id, saved)
        log.error("Missing 'Manage Messages' permission in #%s - couldn't remove %s", message.channel, att.filename)
        await react(message, REACT_REVIEW)
        await log_to_channel(
            message.guild.id,
            f"⚠️ Couldn't remove an AI image from {message.author.mention} in {message.channel.mention} — "
            f"I'm missing **Manage Messages** there. Reason: {reason}\n{message.jump_url}")
        return
    except discord.NotFound:
        # The member deleted it first - nothing to count or restore
        forget_detection(det_id, saved)
        log.info("Message %s was deleted before the bot could remove it", message.id)
        return
    log_event("REMOVED", message.guild, message.channel, str(message.author), message.author.id, det_id, att.filename, reason)

    async def notify():
        if NOTIFY_USER:
            try:
                await message.channel.send(removal_notice(message, hit), delete_after=15)
            except discord.HTTPException:
                pass

    await asyncio.gather(notify(), log_to_channel(
        message.guild.id,
        f"🗑️ **#{det_id}** Removed `{att.filename}` from {message.author.mention} in "
        f"{message.channel.mention}\nReason: {reason}"
        + ("" if saved else "\n*(no restorable copy was saved)*"),
        view=restore_view(det_id) if saved else None,
    ))


async def handle(message: discord.Message, skip: set[str] | None = None):
    if is_exempt(message):
        return
    images = [src for src in collect_sources(message) if source_key(src) not in (skip or set())]
    if not images:
        return
    links = [src for src in images if isinstance(src, LinkImage)]

    t0 = time.perf_counter()
    downloaded: dict[int, bytes] = {}
    results = []
    # One image at a time: download, check, and stop at the first image that fails
    for att in images:
        try:
            data = await att.read(use_cached=False)
            downloaded[id(att)] = data
            verdict, hit = await scan_image(data, att, message.guild.id)
        except Exception:
            log.exception("Scan failed for %s", att.filename)
            verdict, hit = UNKNOWN, None

        if is_removal(verdict, hit):
            log.info("FLAG %s from %s in #%s: [%s] %s (%.0f ms)", att.filename, message.author,
                     message.channel, hit[0], hit[1], (time.perf_counter() - t0) * 1000)
            await remove(message, att, hit, downloaded, links)
            return
        results.append((att, verdict, hit[1] if hit else None))

    log.info("%s from %s in #%s (%.0f ms)", ", ".join(f"{a.filename}={v.upper()}" for a, v, _ in results),
             message.author, message.channel, (time.perf_counter() - t0) * 1000)

    suspects = [(a, r) for a, v, r in results if v == SUSPECT]
    if suspects:
        await react(message, REACT_REVIEW)
        for att, reason in suspects:
            det = record(message, att, reason, "review")
            log_event("REVIEW", message.guild, message.channel, str(message.author), message.author.id, det, att.filename, reason)
            await log_to_channel(
                message.guild.id,
                f"⚠️ Possible AI image `{att.filename}` from {message.author.mention} in "
                f"{message.channel.mention} — {reason}\n{message.jump_url}"
            )
    elif any(v == UNKNOWN for _, v, _ in results):
        await react(message, REACT_UNKNOWN)
    elif REACT_CLEAN:
        await react(message, REACT_OK)


@client.event
async def on_message(message: discord.Message):
    await handle(message)


@client.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    # Discord adds link previews (embeds) a moment after the message arrives, as an edit.
    # Only check what's new, so nothing gets scanned twice.
    await handle(after, skip={source_key(s) for s in collect_sources(before)})


# ------------------------------------------------------- mod commands ---
ACTION_LABEL = {"removed": "🗑️ removed", "test": "🤖 test mode", "review": "⚠️ review",
                "restored": "♻️ restored by a mod"}


def since_clause(days: int | None) -> tuple[str, list]:
    if days and days > 0:
        return " AND ts >= ?", [int(time.time()) - days * 86400]
    return "", []


@tree.command(name="ai-offenders", description="Who has posted AI images, and how many")
@app_commands.describe(days="Only count the last N days (leave empty for all time)")
@app_commands.default_permissions(manage_messages=True)
@app_commands.guild_only()
async def ai_offenders(interaction: discord.Interaction, days: app_commands.Range[int, 1, 3650] | None = None):
    where, args = since_clause(days)
    rows = db.execute(
        f"""SELECT user_id, MAX(user_name),
                   SUM(action IN ('removed','test')), SUM(action = 'review'), MAX(ts)
            FROM detections WHERE guild_id = ?{where} AND action IN ('removed','test','review')
            GROUP BY user_id
            ORDER BY SUM(action IN ('removed','test')) DESC, SUM(action = 'review') DESC
            LIMIT 25""",
        [interaction.guild_id, *args],
    ).fetchall()
    period = f"last {days} days" if days else "all time"
    if not rows:  # restored posts don't count, so members with only restores don't appear
        when = f"in the last {days} day{'s' if days != 1 else ''}" if days else "yet"
        embed = discord.Embed(
            title="✅ No AI image posters",
            description=(f"Nobody has posted an AI image {when}.\n\n"
                         "Restored posts (false positives and relevant images) don't count, "
                         "so they aren't listed here."),
            color=discord.Color.green(),
        )
        embed.set_footer(text=f"Period: {period}")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    lines = [
        f"**{i}.** <@{uid}> (`{name}`) — "
        + ", ".join(p for p in (
            f"**{hits}** AI image{'s' if hits != 1 else ''}" if hits else "",
            f"{reviews} flagged for review" if reviews else "") if p)
        + f" · last <t:{last}:R>"
        for i, (uid, name, hits, reviews, last) in enumerate(rows, 1)
    ]
    embed = discord.Embed(title=f"AI image posters — {period}", description="\n".join(lines),
                          color=discord.Color.orange())
    embed.set_footer(text="Use /ai-history @user for details")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="ai-history", description="Show a member's AI image detections")
@app_commands.describe(member="Who to look up")
@app_commands.default_permissions(manage_messages=True)
@app_commands.guild_only()
async def ai_history(interaction: discord.Interaction, member: discord.User):
    gid = interaction.guild_id
    hits, reviews = db.execute(
        "SELECT COALESCE(SUM(action IN ('removed','test')),0), COALESCE(SUM(action='review'),0)"
        " FROM detections WHERE guild_id=? AND user_id=?", (gid, member.id),
    ).fetchone()
    if not hits and not reviews:
        await interaction.response.send_message(f"{member.mention} has no AI images on record. ✅", ephemeral=True)
        return
    recent = db.execute(
        "SELECT id, ts, channel_id, filename, reason, action, saved_dir, restore_reason FROM detections"
        " WHERE guild_id=? AND user_id=? ORDER BY ts DESC LIMIT 10", (gid, member.id),
    ).fetchall()
    lines = [
        f"`#{did}` <t:{ts}:d> <#{ch}> · {ACTION_LABEL.get(action, action)}"
        + (f" ({RESTORE_REASONS[why][0].lower()})" if action == "restored" and why in RESTORE_REASONS else "")
        + f" · `{fn[:40]}`"
        + (" · restorable" if action == "removed" and saved else "")
        + f"\n└ {reason[:120]}"
        for did, ts, ch, fn, reason, action, saved, why in recent
    ]
    embed = discord.Embed(
        title=f"AI image history — {member}",
        description=f"**{hits}** AI image{'s' if hits != 1 else ''}"
                    + (f", **{reviews}** flagged for review" if reviews else "")
                    + "\n\n**Most recent:**\n" + "\n".join(lines),
        color=discord.Color.orange(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text="Restore a removed post with /ai-restore <number> <reason>")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="ai-reset", description="Clear AI image records for one member, or everyone")
@app_commands.describe(member="Whose records to clear (leave empty to clear the whole server)",
                       test_only="Only clear entries recorded in test mode (DRY_RUN)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def ai_reset(interaction: discord.Interaction, member: discord.User | None = None, test_only: bool = False):
    sql, args = "DELETE FROM detections WHERE guild_id=?", [interaction.guild_id]
    if member:
        sql += " AND user_id=?"
        args.append(member.id)
    if test_only:
        sql += " AND action='test'"
    n = db.execute(sql, args).rowcount
    db.commit()
    who = member.mention if member else "everyone"
    await interaction.response.send_message(
        f"Cleared {n} record{'s' if n != 1 else ''} for {who}{' (test mode only)' if test_only else ''}.",
        ephemeral=True,
    )


# ------------------------------------------------- public /ai-check ---
CHECK_COOLDOWN = int(os.getenv("CHECK_COOLDOWN_SECONDS", "30"))  # per user, protects API quotas

KIND_LABEL = {
    "gemini": "Google Gemini", "chatgpt": "ChatGPT / OpenAI", "synthid": "SynthID watermark",
    "c2pa": "AI Content Credentials", "stable_diffusion": "Stable Diffusion",
    "generator": "AI generator metadata", "iptc": "AI-generated metadata tag", "classifier": "AI-image classifier",
}


@tree.command(name="ai-check", description="Privately check an image for AI watermarks — nothing is deleted or recorded")
@app_commands.describe(image="The image to check")
@app_commands.guild_only()
@app_commands.checks.cooldown(1, CHECK_COOLDOWN, key=lambda i: i.user.id)
async def ai_check(interaction: discord.Interaction, image: discord.Attachment):
    mime = mime_of(image)
    if mime not in IMAGE_TYPES:
        await interaction.response.send_message("That isn't a supported image (PNG, JPEG, WebP, GIF, AVIF or HEIC).",
                                                ephemeral=True)
        return
    if image.size > MAX_BYTES:
        await interaction.response.send_message(f"That image is too large to check ({MAX_FILE_MB} MB max).",
                                                ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    # Private by design: /ai-check never calls record() or log_to_channel(), so no
    # result (including ⚠️ unsure or ❔) is saved, counted, or shown to mods.
    t0 = time.perf_counter()
    try:
        data = await image.read(use_cached=False)
        verdict, hit = await scan_image(data, image, interaction.guild_id, PRIORITY_CHECK)
    except Exception:
        log.exception("/ai-check failed for %s", interaction.user)
        verdict, hit = UNKNOWN, None
    ms = (time.perf_counter() - t0) * 1000
    log.info("/ai-check by %s: %s %s (%.0f ms)", interaction.user, image.filename, verdict.upper(), ms)

    if verdict == FLAG or (verdict == SUSPECT and CLASSIFIER_ACTION == "delete"):
        title, color = "🤖 This would be removed", discord.Color.red()
        desc = (f"**Caught by:** {KIND_LABEL.get(hit[0], hit[0])}\n**Details:** {hit[1]}\n\n"
                "If you post it here, the bot will delete it.")
    elif verdict == SUSPECT:
        title, color = "⚠️ Unsure — might be AI-generated", discord.Color.gold()
        desc = (f"**Details:** {hit[1]}\n\nNo watermark was found, but the AI-image classifier thinks it "
                "looks AI-made. This check is private: no mods were contacted.")
    elif verdict == UNKNOWN:
        title, color = "❔ Couldn't finish checking", discord.Color.light_grey()
        desc = ("One of the online checks didn't respond, so the result is incomplete. "
                "This check is private: no mods were contacted. Try again in a minute.")
    else:
        title, color = "✅ No AI markers found", discord.Color.green()
        desc = ("This image passed every check. A pass means no AI watermark or tag was found, not that "
                "it's proven to be human-made: screenshots and edits can strip those markers.")
    embed = discord.Embed(title=title, description=desc, color=color)
    embed.set_footer(text=f"{image.filename} · checked in {ms / 1000:.1f}s · only you can see this · nothing was saved")
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="ai-help", description="List the AI image bot's commands")
@app_commands.guild_only()
async def ai_help(interaction: discord.Interaction):
    perms = interaction.permissions
    embed = discord.Embed(
        title="🤖 AI Image Checker — commands",
        description=("I check images posted here for AI watermarks and metadata (Gemini, ChatGPT, "
                     "Stable Diffusion and more) and remove AI-generated ones."),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="For everyone",
        value=("`/ai-check image:` — privately check an image before posting it. Nothing is deleted, "
               "saved, or sent to mods.\n"
               "`/ai-help` — show this list."),
        inline=False,
    )
    if perms.manage_messages:
        embed.add_field(
            name="For mods",
            value=("`/ai-offenders [days]` — who has posted AI images, and how many\n"
                   "`/ai-history @member` — a member's detections and their numbers\n"
                   "`/ai-restore <number> <reason>` — put back a removed post\n"
                   "　• **False positive**: not AI, never removed again\n"
                   "　• **Relevant**: AI, but allowed this time\n"
                   "You can also use the ♻️ buttons in the log channel."),
            inline=False,
        )
    if perms.manage_guild:
        embed.add_field(
            name="For admins",
            value=("`/ai-log-channel [#channel]` — choose where removals are posted for mods\n"
                   "`/ai-reset [@member] [test_only]` — clear records for one member or the whole server"),
            inline=False,
        )
    embed.add_field(
        name="Reactions on posts",
        value="✅ passed  ·  ⚠️ might be AI (mods will review)  ·  ❔ couldn't finish checking",
        inline=False,
    )
    embed.set_footer(text=f"Discord AI Image Block v{VERSION} · only you can see this")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        msg = f"Slow down a little. You can check another image in {error.retry_after:.0f}s."
    else:
        log.error("Command error in /%s: %s", interaction.command.name if interaction.command else "?", error)
        msg = "Something went wrong running that command."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


# ------------------------------------------------------------- restore ---
# Why a mod restored a post:
#   false_positive = the image isn't AI; its exact file is remembered so it's never removed again
#   relevant       = it IS AI, but allowed this once; the same file posted again gets checked normally
RESTORE_REASONS = {
    "false_positive": ("False positive", "not AI (false positive)"),
    "relevant": ("Relevant", "allowed by a mod as relevant"),
}


# Discord rejects webhook names containing "discord", so the restore webhook drops it.
WEBHOOK_NAME = "AI Image Block Restore"
OLD_WEBHOOK_NAMES = {"SynthID Guard Restore"}  # created by earlier versions; still reused
_webhooks: dict[int, discord.Webhook] = {}


async def restore_webhook(channel) -> discord.Webhook | None:
    """The bot's own webhook in this channel (threads use their parent's). None = no permission."""
    target = channel.parent if isinstance(channel, discord.Thread) else channel
    if target.id in _webhooks:
        return _webhooks[target.id]
    try:
        hook = next((h for h in await target.webhooks()
                     if (h.name == WEBHOOK_NAME or h.name in OLD_WEBHOOK_NAMES)
                     and h.token and h.user and h.user.id == client.user.id), None)
        if hook is None:
            hook = await target.create_webhook(name=WEBHOOK_NAME, reason="Restoring posts removed by Discord AI Image Block")
    except (discord.Forbidden, AttributeError):
        return None
    _webhooks[target.id] = hook
    return hook


def webhook_username(name: str) -> str:
    # Discord rejects webhook names containing "discord" or "clyde"
    name = re.sub(r"(?i)discord", "Disc0rd", re.sub(r"(?i)clyde", "Clyd3", name or "")).strip()
    return (name or "Unknown member")[:80]


def split_text(text: str, limit: int = 2000) -> list[str]:
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks + [text]


_restoring: set[int] = set()


async def restore_detection(guild: discord.Guild, det_id: int, mod: discord.abc.User,
                            reason: str) -> tuple[bool, str]:
    # Two mods clicking Restore at the same moment must not repost the post twice
    if det_id in _restoring:
        return False, f"**#{det_id}** is already being restored by another mod."
    _restoring.add(det_id)
    try:
        return await _restore_detection(guild, det_id, mod, reason)
    finally:
        _restoring.discard(det_id)


async def _restore_detection(guild: discord.Guild, det_id: int, mod: discord.abc.User,
                             reason: str) -> tuple[bool, str]:
    if reason not in RESTORE_REASONS:
        return False, "Pick a reason: false positive or relevant."
    row = db.execute(
        "SELECT user_id, channel_id, action, saved_dir, message_text, author_name, avatar_url, posted_ts,"
        " reply_url, user_name FROM detections WHERE id=? AND guild_id=?",
        (det_id, guild.id),
    ).fetchone()
    if not row:
        return False, f"No detection **#{det_id}** in this server."
    user_id, channel_id, action, saved_dir, text, author_name, avatar_url, posted_ts, reply_to, user_name = row
    if action == "restored":
        return False, f"**#{det_id}** was already restored."
    if action != "removed":
        return False, f"**#{det_id}** was never deleted ({ACTION_LABEL.get(action, action)}), so there's nothing to restore."
    if not saved_dir or not os.path.isdir(saved_dir):
        return False, (f"No saved copy of **#{det_id}** — it's older than {QUARANTINE_DAYS} days "
                       "or couldn't be saved at the time.")

    channel = guild.get_channel_or_thread(channel_id)
    if channel is None:
        return False, "The original channel no longer exists."

    paths = sorted(os.path.join(saved_dir, f) for f in os.listdir(saved_dir))
    post_paths = [p for p in paths if not os.path.basename(p).startswith("L")]  # links come back via the text
    # Bots can only upload up to the server's limit (higher with boosts); bigger files can't be reposted
    limit = getattr(guild, "filesize_limit", 10 * 1024 * 1024)
    too_big = [os.path.basename(p)[3:] for p in post_paths if os.path.getsize(p) > limit]
    post_paths = [p for p in post_paths if os.path.getsize(p) <= limit]

    footer = (f"-# ♻️ Restored by {mod.mention} · {RESTORE_REASONS[reason][0].lower()} · "
              f"originally posted <t:{posted_ts or int(time.time())}:f>")
    if too_big:
        footer += f"\n-# ⚠️ Too large for this server's upload limit, not restored: {', '.join(too_big)[:200]}"
    head = f"-# ↪ Replying to {reply_to}\n" if reply_to else ""
    chunks = split_text(head + (text or "") + ("\n" if text else "") + footer)
    no_ping = discord.AllowedMentions.none()

    hook = await restore_webhook(channel)
    method = "as the original member" if hook else "by the bot (no Manage Webhooks permission)"
    files = [discord.File(p, filename=os.path.basename(p)[3:]) for p in post_paths]  # strip "00_" prefix
    try:
        for i, chunk in enumerate(chunks):
            kw = {"files": files} if i == len(chunks) - 1 and files else {}
            if hook:
                if isinstance(channel, discord.Thread):
                    kw["thread"] = channel
                await hook.send(chunk, username=webhook_username(author_name or user_name),
                                avatar_url=avatar_url or discord.utils.MISSING,
                                allowed_mentions=no_ping, wait=True, **kw)
            else:
                prefix = f"♻️ **Restored post** from <@{user_id}>\n" if i == 0 else ""
                await channel.send(prefix + chunk, allowed_mentions=no_ping, **kw)
    except discord.Forbidden:
        return False, f"I need **Attach Files** permission in {channel.mention} to restore images."
    except discord.NotFound:
        _webhooks.pop(getattr(channel, "parent_id", None) or channel.id, None)  # webhook was deleted
        return False, "The restore webhook was deleted. Try again and I'll make a new one."
    except discord.HTTPException as e:
        return False, f"Discord rejected the restore ({e.text or e.status})."
    finally:
        for f in files:
            f.close()

    if reason == "false_positive":
        # Remember these exact files so reposting them never triggers another deletion
        for p in paths:
            with open(p, "rb") as fh:
                allow_file(guild.id, fh.read(), mod.id)
    db.execute("UPDATE detections SET action='restored', restored_by=?, restored_ts=?, restore_reason=?,"
               " saved_dir=NULL WHERE id=?", (mod.id, int(time.time()), reason, det_id))
    db.commit()
    shutil.rmtree(saved_dir, ignore_errors=True)
    log.info("Detection #%s restored by %s (%s)", det_id, mod, reason)
    log_event(f"RESTORED ({RESTORE_REASONS[reason][0].lower()})", guild, channel, user_name, user_id,
              det_id, by=mod)
    extra = (" Those exact images are now remembered and won't be removed again."
             if reason == "false_positive" else
             " The same image posted again will still be checked normally.")
    if too_big:
        extra += f" {len(too_big)} file(s) were too large for this server's upload limit and couldn't be reposted."
    return True, (f"♻️ Restored **#{det_id}** in {channel.mention} {method} "
                  f"({RESTORE_REASONS[reason][0].lower()}). It no longer counts against <@{user_id}>.{extra}")


async def button_restore(interaction: discord.Interaction, det_id: int, reason: str, log_message=None):
    if not interaction.guild or not interaction.permissions.manage_messages:
        await interaction.response.send_message("Only mods (Manage Messages) can restore posts.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    ok, msg = await restore_detection(interaction.guild, det_id, interaction.user, reason)
    log_message = log_message or interaction.message
    if log_message and (ok or "already restored" in msg or "No saved copy" in msg):
        try:  # update the log-channel message and remove the buttons
            note = (f"\n♻️ Restored by {interaction.user.mention} ({RESTORE_REASONS[reason][0].lower()}) "
                    f"<t:{int(time.time())}:R>" if ok else "")
            await log_message.edit(content=log_message.content + note, view=None,
                                   allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass
    await interaction.followup.send(msg, ephemeral=True)


class RestoreButton(discord.ui.DynamicItem[discord.ui.Button],
                    template=r"synthid:restore:(?P<id>[0-9]+)(?::(?P<why>fp|rel))?"):
    """Restore buttons on log-channel removal messages. Survive bot restarts.
    Buttons from before 1.1 have no reason, so they ask for one first."""

    def __init__(self, det_id: int, why: str | None):
        labels = {"fp": ("Restore: false positive", discord.ButtonStyle.success),
                  "rel": ("Restore: relevant", discord.ButtonStyle.secondary),
                  None: ("Restore", discord.ButtonStyle.secondary)}
        label, style = labels[why]
        super().__init__(discord.ui.Button(label=label, emoji="♻️", style=style,
                                           custom_id=f"synthid:restore:{det_id}" + (f":{why}" if why else "")))
        self.det_id, self.why = det_id, why

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["id"]), match["why"])

    async def callback(self, interaction: discord.Interaction):
        if self.why:
            reason = "false_positive" if self.why == "fp" else "relevant"
            await button_restore(interaction, self.det_id, reason)
            return
        # Old single button: ask which reason
        if not interaction.permissions.manage_messages:
            await interaction.response.send_message("Only mods (Manage Messages) can restore posts.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Why are you restoring **#{self.det_id}**?", view=ReasonPicker(self.det_id, interaction.message),
            ephemeral=True)


class ReasonPicker(discord.ui.View):
    def __init__(self, det_id: int, log_message):
        super().__init__(timeout=300)
        self.det_id, self.log_message = det_id, log_message

    @discord.ui.button(label="False positive", emoji="✅", style=discord.ButtonStyle.success)
    async def fp(self, interaction: discord.Interaction, _):
        await button_restore(interaction, self.det_id, "false_positive", self.log_message)

    @discord.ui.button(label="Relevant", emoji="📌", style=discord.ButtonStyle.secondary)
    async def rel(self, interaction: discord.Interaction, _):
        await button_restore(interaction, self.det_id, "relevant", self.log_message)


def restore_view(det_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(RestoreButton(det_id, "fp"))
    view.add_item(RestoreButton(det_id, "rel"))
    return view


client.add_dynamic_items(RestoreButton)


@tree.command(name="ai-restore", description="Put back a post the bot removed")
@app_commands.describe(
    detection="The detection number, e.g. 42 (shown in /ai-history and the log channel)",
    reason="False positive = not AI, never remove this image again. Relevant = AI but allowed this time.",
)
@app_commands.choices(reason=[
    app_commands.Choice(name="False positive (not AI — remember it)", value="false_positive"),
    app_commands.Choice(name="Relevant (AI, but allowed this time)", value="relevant"),
])
@app_commands.default_permissions(manage_messages=True)
@app_commands.guild_only()
async def ai_restore(interaction: discord.Interaction, detection: int, reason: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)
    ok, msg = await restore_detection(interaction.guild, detection, interaction.user, reason.value)
    await interaction.followup.send(msg, ephemeral=True)
    if ok:
        await log_to_channel(interaction.guild_id, f"♻️ {interaction.user.mention} restored **#{detection}** "
                             f"({RESTORE_REASONS[reason.value][0].lower()})")


@tree.command(name="ai-log-channel", description="Choose where removals and reviews are posted for mods")
@app_commands.describe(channel="The channel to use (leave empty to see the current one)",
                       turn_off="Stop posting removals to a channel")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def ai_log_channel(interaction: discord.Interaction,
                         channel: discord.TextChannel | None = None, turn_off: bool = False):
    gid = interaction.guild_id
    if turn_off:
        db.execute("INSERT INTO settings (guild_id, log_channel_id) VALUES (?, 0) "
                   "ON CONFLICT(guild_id) DO UPDATE SET log_channel_id=0", (gid,))
        db.commit()
        await interaction.response.send_message("Removals will no longer be posted to a channel. "
                                                "They're still written to the removal log file.", ephemeral=True)
        return
    if channel is None:
        cid = log_channel_id(gid)
        where = f"<#{cid}>" if cid else "not set"
        file_note = f"\nRemoval log file on the server: `{REMOVAL_LOG_FILE}`" if REMOVAL_LOG_FILE else ""
        await interaction.response.send_message(f"Log channel: {where}{file_note}", ephemeral=True)
        return
    perms = channel.permissions_for(interaction.guild.me)
    if not (perms.view_channel and perms.send_messages):
        await interaction.response.send_message(f"I can't post in {channel.mention}. Give me **View Channel** "
                                                "and **Send Messages** there first.", ephemeral=True)
        return
    db.execute("INSERT INTO settings (guild_id, log_channel_id) VALUES (?, ?) "
               "ON CONFLICT(guild_id) DO UPDATE SET log_channel_id=excluded.log_channel_id", (gid, channel.id))
    db.commit()
    await interaction.response.send_message(f"Removals, reviews, and restores will be posted in {channel.mention}.",
                                            ephemeral=True)
    await log_to_channel(gid, f"📋 {interaction.user.mention} set this as the AI image log channel.")


@tasks.loop(hours=24)
async def purge_quarantine():
    cutoff = int(time.time()) - QUARANTINE_DAYS * 86400
    rows = db.execute("SELECT id, saved_dir FROM detections WHERE saved_dir IS NOT NULL AND ts < ?",
                      (cutoff,)).fetchall()
    for det_id, folder in rows:
        shutil.rmtree(folder, ignore_errors=True)
        db.execute("UPDATE detections SET saved_dir=NULL WHERE id=?", (det_id,))
    db.commit()
    if rows:
        log.info("Purged %d saved posts older than %d days", len(rows), QUARANTINE_DAYS)


@client.event
async def on_guild_join(guild: discord.Guild):
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)


if __name__ == "__main__":
    try:  # faster event loop on Linux
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except ImportError:
        pass

    client.run(TOKEN, log_handler=None)
