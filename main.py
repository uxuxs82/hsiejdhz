import asyncio
import json
import logging
import os
import re
import random
import socket
import time
import urllib.request
import urllib.error
from collections import deque, defaultdict
from glob import glob

from telethon import TelegramClient, events, utils, functions, types
from telethon.sessions import StringSession
from telethon.errors import (
    FloodWaitError, UserAlreadyParticipantError,
    ChatAdminRequiredError, MessageNotModifiedError,
)
from telethon.tl.functions.messages import (
    ImportChatInviteRequest, CheckChatInviteRequest, SendReactionRequest,
)
from telethon.tl.functions.channels import (
    JoinChannelRequest, LeaveChannelRequest, GetFullChannelRequest,
)
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.tl.functions.photos import UploadProfilePhotoRequest

from aiogram import Bot, Dispatcher, F, types as at
from aiogram.filters import Command
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

try:
    from pytgcalls import PyTgCalls
    try:
        from pytgcalls.types import MediaStream
    except Exception:
        from pytgcalls.types.input_stream import AudioPiped as MediaStream
    PYTGCALLS_OK = True
except Exception as _e:
    PYTGCALLS_OK = False
    PYTG_ERR = str(_e)

socket.setdefaulttimeout(20)

# ==================== ENV ====================
# ==== все секреты ТОЛЬКО из env ====
# ==== всё ТОЛЬКО из env ====
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
ANYMODEL_API_KEY = os.environ.get("ANYMODEL_API_KEY", "")
ANYMODEL_BASE_URL = os.environ.get("ANYMODEL_BASE_URL", "https://anymodel.org/v1")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "297562307"))
MY_NAME = os.environ.get("MY_NAME", "vortex")

# Vortex - из TG_STRING_SESSION
TG_STRING_SESSION = os.environ.get("TG_STRING_SESSION", "")
AI_SESSION_ID = os.environ.get("AI_SESSION_ID", "8284866397")
AI_NAME = MY_NAME

# Другие сессии - из SESSIONS_JSON (словарь {id: string})
SESSIONS_JSON = os.environ.get("SESSIONS_JSON", "")

DATA_DIR = "/data"
try: os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    DATA_DIR = "/tmp"
    os.makedirs(DATA_DIR, exist_ok=True)

SILENT_OGG = os.path.join(DATA_DIR, "silent.ogg")
try:
    if not os.path.exists(SILENT_OGG):
        os.system(
            f'ffmpeg -y -f lavfi -i anullsrc=r=48000:cl=mono -t 3600 '
            f'-c:a libopus -b:a 16k "{SILENT_OGG}" > /dev/null 2>&1'
        )
except Exception as _e:
    print(f"silent gen: {_e}")


# ==== громкость ====
VOLUME_LOUD = {"on": True}
VOLUME_BOOST = 5.0


MEDIA_DIR = os.path.join(DATA_DIR, "media")
try:
    os.makedirs(MEDIA_DIR, exist_ok=True)
except Exception:
    MEDIA_DIR = DATA_DIR


def list_media_files():
    if not os.path.exists(MEDIA_DIR):
        return []
    fs = [f for f in os.listdir(MEDIA_DIR)
          if not f.endswith(".tg.mp4")]
    fs.sort(key=lambda f: os.path.getmtime(os.path.join(MEDIA_DIR, f)), reverse=True)
    return fs


def media_kind(path):
    """image | video | gif | sticker | unknown"""
    low = path.lower()
    if low.endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp")):
        return "image"
    if low.endswith(".gif"):
        return "gif"
    if low.endswith((".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts")):
        return "video"
    if low.endswith(".tgs"):
        return "tgs"
    return "unknown"


async def convert_media(path):
    """Приводит любой файл к mp4 H.264 480p для войса (Railway-friendly)."""
    kind = media_kind(path)
    out = path + ".tg.mp4"
    try:
        if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(path):
            return out
    except Exception:
        pass

    if kind == "tgs":
        return None

    import subprocess

    def _run(cmd, timeout):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout)
            if r.returncode != 0:
                err = r.stderr.decode(errors="ignore")[-500:]
                log.warning(f"ffmpeg fail: {err}")
                return False, err
            return True, ""
        except subprocess.TimeoutExpired:
            return False, "timeout"
        except Exception as e:
            return False, str(e)

    try:
        if kind == "image":
            cmd = ["ffmpeg", "-y", "-loop", "1", "-i", path, "-t", "5",
                   "-vf", "scale=480:-2,fps=24,format=yuv420p",
                   "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                   "-movflags", "+faststart", "-threads", "1", out]
            ok, err = await asyncio.get_event_loop().run_in_executor(None, _run, cmd, 120)
        else:
            cmd = ["ffmpeg", "-y", "-i", path,
                   "-vf", "scale=480:-2,fps=24",
                   "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                   "-c:a", "aac", "-b:a", "64k", "-ac", "1",
                   "-movflags", "+faststart", "-threads", "1", out]
            ok, err = await asyncio.get_event_loop().run_in_executor(None, _run, cmd, 600)
        if ok and os.path.exists(out) and os.path.getsize(out) > 500:
            return out
        log.warning(f"convert_media failed: {err}")
        return None
    except Exception as e:
        log.warning(f"convert_media: {e}")
        return None



def mp3_duration(path):
    """Длительность медиа в секундах через ffprobe."""
    try:
        import subprocess
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stderr=subprocess.DEVNULL, timeout=15
        ).decode().strip()
        return float(out)
    except Exception as e:
        log.warning(f"ffprobe: {e}")
        return 0


def list_voice_files():
    vd = os.path.join(DATA_DIR, "voice")
    if not os.path.exists(vd):
        return []
    exts = (".mp3", ".ogg", ".oga", ".opus", ".m4a", ".wav", ".aac", ".flac")
    fs = [f for f in os.listdir(vd) if f.lower().endswith(exts)]
    fs.sort(key=lambda f: os.path.getmtime(os.path.join(vd, f)), reverse=True)
    return fs


async def make_loud(path):
    """Если включён режим ГРОМКО — возвращает путь к громкой копии."""
    if not VOLUME_LOUD.get("on"):
        return path
    out = path + ".loud3.mp3"
    try:
        if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(path):
            return out
    except Exception:
        pass
    af = (
        "acompressor=threshold=-25dB:ratio=12:attack=3:release=60:makeup=12,"
        "loudnorm=I=-5:TP=-0.1:LRA=3,"
        "alimiter=limit=0.99,"
        "volume=2.5"
    )
    try:
        import subprocess
        def _run():
            subprocess.check_output(
                ["ffmpeg", "-y", "-i", path,
                 "-af", af,
                 "-ar", "48000", "-ac", "1",
                 "-c:a", "libmp3lame", "-b:a", "192k", out],
                stderr=subprocess.DEVNULL, timeout=180,
            )
        await asyncio.get_event_loop().run_in_executor(None, _run)
        return out
    except Exception as e:
        log.warning(f"make_loud: {e}")
        return path

WATCH_CHATS = [-1002828764783, "@BhopProChat"]
IGNORED_BOTS = ["valyutaTG_bot","themetrbot","iris_bs_bot","iris_cm_bot","ZanAIsuka_bot","MarvelVoiceBot"]
NO_BUTTON_BOTS = ["iris_bs_bot","iris_cm_bot","valyutaTG_bot","MarvelVoiceBot"]

TRIGGER_EVERY = 3
IDLE_TRIGGER_SEC = 60
CONTEXT_SIZE = 50
HISTORY_SIZE = 100
POLL_INTERVAL = 1
POLL_LIMIT = 15
AI_TIMEOUT = 30
MAX_AI_TOTAL = 90

DEFAULT_MODELS = [
    "qwen/qwen3.7-max","glm/glm-5.3-flash","ds/deepseek-v4-flash",
    "qwen/qwen3.8-max","qwen/qwen3.7-plus","ag/gemini-2.5-flash",
    "ag/gemini-pro-agent","kmc/kimi-for-coding","glm/glm-5.2",
    "glm/glm-5.1","glm/glm-5","glm/glm-4.7","cx/gpt-5.6-luna","cx/gpt-6-luna",
]
MODELS_FILE = os.path.join(DATA_DIR, "models.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
MEMORY_FILE = os.path.join(DATA_DIR, "memory.json")

MEMORY_BOT_MSGS = 200
MEMORY_USERS = 100
MEMORY_DIALOG = 15
MEMORY_SEEN = 5000

STOP_WORDS = {"дроч","хм","эм","мм","прив","привет","ку","да","нет","ну","ага","угу","ок","окей","лол","ахах","хаха","кек","жиза","ору","ржу","жесть","пиздец","бля","блять","чо","чё","шо","?","...",".",",","-","+","=","*","/"}
BAD_WORDS = ["pollinations","subscribe","подпишись","реклам","промокод","casino","казино","ставк","букмекер"]
BAD_MARKERS = ["we need","the format","response to","as an ai","i cannot","i can't","as a language model","user asks","let me"]

SYSTEM_PROMPT = """ты школьник 15 лет, тебя зовут vortex. отвечай на сообщение в чате.

как читать чат:
- сообщения идут по порядку: #1, #2, #3...
- (БОТ) = игровой бот, не человек
- (ОТВЕЧАЕТ имя) = человек отвечает ЭТОМУ имени, не тебе

ГЛАВНОЕ:
- НЕ выдумывай факты, имена, события которых нет
- если не уверен - не утверждай, спроси или пошути
- 'решение: нет' пиши только если реально нечего сказать

стиль:
- строчными, без точек и запятых
- максимум 12 слов
- живой школьник с телефона
- не признавайся что ты ии
- НЕ пиши 'бро', 'друг', 'дружище'
- можно мат, но в меру

формат:
решение: <номер>
сообщения:
<текст>

или:
решение: нет
"""

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("main")

# ==================== STATE ====================
CLIENTS = {}
CLIENT_META = {}
VOICE_CALLS = {}
ADMIN_STATE = {}
MODELS = list(DEFAULT_MODELS)
SETTINGS = {"ai_enabled": True, "trigger_every": TRIGGER_EVERY}

if os.path.exists(MODELS_FILE):
    try:
        with open(MODELS_FILE) as f: MODELS = json.load(f)
    except: pass
if os.path.exists(SETTINGS_FILE):
    try:
        with open(SETTINGS_FILE) as f: SETTINGS.update(json.load(f))
    except: pass
try:
    VOLUME_LOUD["on"] = bool(SETTINGS.get("volume_loud", False))
except Exception:
    pass

bot = Bot(BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher() if bot else None


def save_json(path, obj):
    try:
        with open(path, "w") as f: json.dump(obj, f, ensure_ascii=False)
    except: pass


# ==================== СЕССИИ ====================
def scan_env_sessions():
    """Возвращает {sid: string_session}.
    1) vortex из TG_STRING_SESSION
    2) остальные из SESSIONS_JSON (словарь)"""
    out = {}
    if TG_STRING_SESSION:
        out[AI_SESSION_ID] = TG_STRING_SESSION.strip()
    if SESSIONS_JSON:
        try:
            d = json.loads(SESSIONS_JSON)
            for k, v in d.items():
                if v:
                    out[str(k)] = str(v)
        except Exception as e:
            log.warning(f"SESSIONS_JSON parse: {e}")
    return out
    return {str(k): str(v) for k, v in CFG.get("SESSIONS", {}).items() if v}


async def load_env_sessions():
    sess = scan_env_sessions()
    log.info(f"env сессий: {len(sess)}")
    for sid, ss in sess.items():
        try:
            c = TelegramClient(StringSession(ss), API_ID, API_HASH)
            await c.connect()
            if not await c.is_user_authorized():
                log.warning(f"{sid}: не авторизована")
                await c.disconnect(); continue
            me = await c.get_me()
            CLIENTS[sid] = c
            CLIENT_META[sid] = {"name": me.first_name or str(me.id), "id": me.id}
            log.info(f"✓ {sid} -> {me.first_name} (id {me.id})")
        except Exception as e:
            log.warning(f"{sid}: {e}")
    log.info(f"онлайн: {len(CLIENTS)}")


def list_sids():
    return sorted(CLIENTS.keys())


# ==================== ССЫЛКИ ====================
def parse_target(s):
    s = s.strip()
    # t.me/chat?videochat, t.me/chat/123?comment=1 -> чистим query и fragment
    s = s.split("?")[0].split("#")[0].rstrip("/")
    m = re.match(r"(?:https?://)?t\.me/(?:joinchat/|\+)([A-Za-z0-9_\-]+)", s)
    if m: return "invite", m.group(1)
    m = re.match(r"(?:https?://)?t\.me/([A-Za-z0-9_]+)/(\d+)", s)
    if m: return "msg", (m.group(1), int(m.group(2)))
    m = re.match(r"(?:https?://)?t\.me/([A-Za-z0-9_]+)/?$", s)
    if m: return "user", m.group(1)
    m = re.match(r"@([A-Za-z0-9_]+)$", s)
    if m: return "user", m.group(1)
    try: return "id", int(s)
    except: pass
    if re.match(r"^[A-Za-z0-9_]{4,}$", s): return "user", s
    return None, None


async def resolve_entity(client, raw):
    kind, val = parse_target(raw)
    if kind is None: return None
    try:
        if kind == "invite":
            try:
                upd = await client(ImportChatInviteRequest(val))
                ch = getattr(upd, "chats", [])
                if ch: return ch[0]
            except UserAlreadyParticipantError:
                pass
            try:
                info = await client(CheckChatInviteRequest(val))
                if getattr(info, "chat", None): return info.chat
            except: pass
            return None
        elif kind == "msg":
            return await client.get_entity(val[0])
        else:
            return await client.get_entity(val)
    except Exception as e:
        log.warning(f"resolve {raw}: {e}")
        return None


def _get_voice_chat_id(full_chat):
    """Возвращает call id голосового чата из full_chat."""
    try:
        call = getattr(full_chat, "call", None)
        if call:
            return call.id
    except Exception:
        pass
    return None


# ==================== ИИ ====================
class Memory:
    def __init__(self):
        self.bot_messages = defaultdict(lambda: deque(maxlen=MEMORY_BOT_MSGS))
        self.users = defaultdict(lambda: deque(maxlen=MEMORY_USERS))
        self.dialogs = defaultdict(lambda: defaultdict(lambda: deque(maxlen=MEMORY_DIALOG)))
        self.replied_ids = set()
        self.seen = defaultdict(set)
        self.seen_order = defaultdict(deque)
        self.load()

    def load(self):
        if not os.path.exists(MEMORY_FILE): return
        try:
            with open(MEMORY_FILE) as f: d = json.load(f)
            for cid, msgs in d.get("bot_messages", {}).items():
                self.bot_messages[int(cid)] = deque(msgs, maxlen=MEMORY_BOT_MSGS)
            for cid, dialogs in d.get("dialogs", {}).items():
                for uid, msgs in dialogs.items():
                    self.dialogs[int(cid)][int(uid)] = deque(msgs, maxlen=MEMORY_DIALOG)
            self.replied_ids = set(d.get("replied_ids", []))
            for cid, ids in d.get("seen", {}).items():
                cid = int(cid); self.seen[cid] = set(ids)
                self.seen_order[cid] = deque(ids[-MEMORY_SEEN:])
        except Exception as e:
            log.warning(f"mem load: {e}")

    def save(self):
        try:
            with open(MEMORY_FILE, "w") as f:
                json.dump({
                    "bot_messages": {str(k): list(v) for k, v in self.bot_messages.items()},
                    "dialogs": {str(k): {str(u): list(m) for u, m in vv.items()} for k, vv in self.dialogs.items()},
                    "replied_ids": list(self.replied_ids)[-10000:],
                    "seen": {str(c): list(o) for c, o in self.seen_order.items()},
                }, f, ensure_ascii=False)
        except: pass

    def mark_seen(self, cid, mid):
        s = self.seen[cid]
        if mid in s: return False
        s.add(mid); o = self.seen_order[cid]; o.append(mid)
        while len(o) > MEMORY_SEEN: s.discard(o.popleft())
        return True

    def was_seen(self, cid, mid): return mid in self.seen.get(cid, set())
    def mark_replied(self, mid): self.replied_ids.add(mid)
    def was_replied(self, mid): return mid in self.replied_ids
    def add_bot_msg(self, cid, mid, t): self.bot_messages[cid].append({"id": mid, "text": t})

    def is_reply_to_bot(self, cid, rid):
        for m in self.bot_messages[cid]:
            if m["id"] == rid: return m["text"]
        return None

    def add_dialog(self, cid, uid, role, t): self.dialogs[cid][uid].append({"role": role, "text": t})

    def memory_text(self, cid, uid):
        d = self.dialogs[cid][uid]
        parts = []
        if d: parts.append("история:\n" + "\n".join(f"{m['role']}: {m['text']}" for m in d))
        my = list(self.bot_messages.get(cid, []))[-10:]
        if my: parts.append("твои последние:\n" + "\n".join(m["text"] for m in my))
        return "\n\n".join(parts)


class ChatState:
    def __init__(self, cid):
        self.chat_id = cid
        self.counter = 0
        self.num_to_msg, self.num_to_user, self.num_to_bot = {}, {}, {}
        self.num_reply_to, self.num_text = {}, {}
        self.needs_response = False
        self.force_reply_to = None
        self.last_msg_time = 0.0
        self.last_bot_reply_time = 0.0
        self.in_progress = False
        self.last_processed_id = None
        self.human_count = 0

    def add(self, sid, sname, t, mid, is_bot=False, reply_to_id=None):
        self.counter += 1
        n = self.counter
        self.num_to_msg[n], self.num_to_user[n] = mid, (sid, sname)
        self.num_to_bot[n], self.num_reply_to[n] = is_bot, reply_to_id
        self.num_text[n] = t
        self.last_msg_time = time.time()
        while len(self.num_to_msg) > HISTORY_SIZE:
            old = min(self.num_to_msg.keys())
            for d in (self.num_to_msg, self.num_to_user, self.num_to_bot,
                      self.num_reply_to, self.num_text): d.pop(old, None)
        return n

    def msg_num_by_id(self, mid):
        for n, m in self.num_to_msg.items():
            if m == mid: return n
        return None

    def last_human_msg_num(self):
        for n in sorted(self.num_to_msg.keys(), reverse=True):
            uid, _ = self.num_to_user.get(n, (0, "?"))
            if uid == 0: continue
            if not self.num_to_bot.get(n, False): return n
        return None

    def context_text(self, tnum, limit=None):
        ns = [n for n in sorted(self.num_to_msg.keys()) if n <= tnum]
        if limit: ns = ns[-limit:]
        lines = []
        for n in ns:
            s = self.num_to_user.get(n, ("?", "?"))[1]
            t = self.num_text.get(n, "")
            bn = " (БОТ)" if self.num_to_bot.get(n, False) else ""
            rn = ""
            rid = self.num_reply_to.get(n)
            if rid:
                rn_ = self.msg_num_by_id(rid)
                if rn_: rn = f" (ОТВЕЧАЕТ {self.num_to_user.get(rn_, ('?','?'))[1]})"
            mk = " <<< ОТВЕТЬ" if n == tnum else ""
            lines.append(f"#{n} {s}{bn}: {t}{rn}{mk}")
        return "\n".join(lines)


memory = Memory()
chats = {}
ai_lock = asyncio.Lock()


def _extract(data):
    try: m = data["choices"][0]["message"]
    except: return None
    if isinstance(m, dict):
        c = m.get("content")
        if c and isinstance(c, str) and c.strip(): return c.strip()
    return None


def _try_model(model, prompt):
    url = ANYMODEL_BASE_URL + "/chat/completions"
    body = {"model": model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": prompt}],
            "temperature": 0.7, "stream": False, "max_tokens": 400}
    headers = {"Content-Type": "application/json",
               "Authorization": "Bearer " + ANYMODEL_API_KEY,
               "User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=AI_TIMEOUT) as r:
            d = json.loads(r.read().decode())
            t = _extract(d); return (t, None) if t else (None, "empty")
    except urllib.error.HTTPError as e: return None, f"HTTP {e.code}"
    except Exception as e: return None, str(e)


def _ask_sync(prompt):
    t0 = time.time()
    for m in MODELS:
        if time.time() - t0 > MAX_AI_TOTAL: break
        text, err = _try_model(m, prompt)
        if text and ("решение" in text.lower() or "реши" in text.lower()):
            return text
        log.warning(f"[AI] {m}: {err or 'no format'}")
    return None


async def ask_ai(prompt):
    return await asyncio.get_event_loop().run_in_executor(None, _ask_sync, prompt)


def parse_decision(text):
    if not text: return None, []
    num, msgs, mode, cur = None, [], None, []
    for line in text.split("\n"):
        low = line.strip().lower()
        if low.startswith("решение:"):
            v = line.split(":", 1)[1].strip()
            if v.lower() in ("нет","no","none","-"): return None, []
            try: num = int(v.replace("#","").strip())
            except: num = None
        elif low.startswith("сообщения:"): mode = "m"
        elif mode == "m":
            if line.strip() == "---":
                if cur: msgs.append(" ".join(cur).strip()); cur = []
            elif line.strip(): cur.append(line.strip())
    if cur: msgs.append(" ".join(cur).strip())
    msgs = [m for m in msgs if m and m.lower() != "нет"]
    clean = []
    for m in msgs:
        m = m.rstrip(".!?,;:")
        w = m.split()
        if len(w) > 15: m = " ".join(w[:15])
        m = m.replace(".","").replace(",","").replace("!","").replace("?","")
        clean.append(m)
    return (num, clean[:2]) if num and clean else (None, [])


# ==================== ИИ ВОРКЕР ====================
ai_chat_ids = set()


async def ai_poller():
    client = CLIENTS.get(AI_SESSION_ID)
    if not client:
        log.error("vortex не загружен, ИИ отключён"); return
    me = await client.get_me()

    entities = {}
    for name in WATCH_CHATS:
        e = await resolve_entity(client, str(name))
        if e:
            try: await client(JoinChannelRequest(e))
            except: pass
            entities[utils.get_peer_id(e)] = e
            ai_chat_ids.add(utils.get_peer_id(e))
            log.info(f"ИИ слушает {name}")

    # прогрев
    for cid, e in entities.items():
        chats[cid] = ChatState(cid)
        st = chats[cid]
        try:
            msgs = []
            async for m in client.iter_messages(e, limit=100): msgs.append(m)
            msgs.reverse()
            for m in msgs:
                if m.sender_id == me.id: continue
                t = (m.text or "").strip()
                if not t: continue
                memory.mark_seen(cid, m.id)
                try: sender = await m.get_sender()
                except: sender = None
                if not sender: continue
                name_ = getattr(sender, "first_name", None) or getattr(sender, "title", None) or "?"
                uid = getattr(sender, "id", 0)
                un = getattr(sender, "username", "") or ""
                is_b = bool(getattr(sender, "bot", False)) or un in IGNORED_BOTS
                st.add(uid, name_, t, m.id, is_bot=is_b, reply_to_id=m.reply_to_msg_id)
        except Exception as e:
            log.warning(f"preload {cid}: {e}")
        st.last_msg_time = time.time()
        st.last_bot_reply_time = time.time()

    log.info("ИИ воркер запущен")

    while True:
        if not SETTINGS.get("ai_enabled", True):
            await asyncio.sleep(2); continue
        try:
            for cid, e in list(entities.items()):
                try:
                    msgs = []
                    async for m in client.iter_messages(e, limit=POLL_LIMIT): msgs.append(m)
                    msgs.reverse()
                    for m in msgs:
                        await ai_process(client, me, m, cid)
                except Exception as ex:
                    log.error(f"poll {cid}: {ex}")
        except Exception as e:
            log.error(f"ai_poller: {e}")
        await asyncio.sleep(POLL_INTERVAL)


async def ai_process(client, me, msg, cid):
    if memory.was_seen(cid, msg.id): return
    memory.mark_seen(cid, msg.id)
    if cid not in chats: chats[cid] = ChatState(cid)
    st = chats[cid]
    if msg.sender_id == me.id: return
    t = (msg.text or "").strip()
    if not t: return
    try: sender = await msg.get_sender()
    except: sender = None
    name = (getattr(sender, "first_name", None) or getattr(sender, "title", None) or "?") if sender else "?"
    uid = sender.id if sender and hasattr(sender, "id") else 0
    if name.lower() == AI_NAME.lower(): return
    un = getattr(sender, "username", "") if sender else ""
    sb = bool(getattr(sender, "bot", False)) if sender else False
    rt = msg.reply_to_msg_id
    rtt = memory.is_reply_to_bot(cid, rt) if rt else None
    if sb or un in IGNORED_BOTS:
        st.add(uid, name, t, msg.id, is_bot=True, reply_to_id=rt); return
    memory.add_dialog(cid, uid, name, t)
    if rtt:
        n = st.add(uid, name, t, msg.id, reply_to_id=rt)
        st.force_reply_to = (n, rtt, uid, name)
        await ai_do(client, st, cid)
        return
    skip = len(t) < 4 or not any(c.isalnum() for c in t)
    if skip: return
    if rt: st.add(uid, name, t, msg.id, reply_to_id=rt); return
    st.add(uid, name, t, msg.id, reply_to_id=rt)
    st.human_count += 1
    if st.human_count >= SETTINGS.get("trigger_every", TRIGGER_EVERY):
        st.human_count = 0
        await ai_do(client, st, cid)


async def ai_do(client, st, cid):
    if st.in_progress: return
    st.in_progress = True
    try:
        force = st.force_reply_to
        if force: num_f, rtt, uid, uname = force
        else: num_f, rtt, uid, uname = None, None, 0, "?"
        tnum = st.last_human_msg_num()
        if tnum is None: return
        st.last_processed_id = st.num_to_msg.get(tnum)
        ctx = st.context_text(tnum, limit=CONTEXT_SIZE)
        if not ctx.strip(): return
        mem = memory.memory_text(cid, uid)
        prompt = f"это чат, ты {AI_NAME}.\nсообщения:\n{ctx}\n"
        if mem: prompt += f"\n{mem}\n"
        prompt += "\nответь на последнее сообщение. если нечего - 'решение: нет'."
        ans = await ask_ai(prompt)
        if not ans: return
        num_ai, messages = parse_decision(ans)
        if not messages: return
        if num_f is not None: num = num_f
        elif num_ai and num_ai in st.num_to_msg and not st.num_to_bot.get(num_ai):
            num = num_ai
        else: return
        tmid = st.num_to_msg.get(num)
        if not tmid or memory.was_replied(tmid): return
        for i, text in enumerate(messages):
            if any(w in text.lower() for w in BAD_WORDS): continue
            sent = await client.send_message(cid, text, reply_to=tmid if i == 0 else None)
            memory.add_bot_msg(cid, sent.id, text)
            log.info(f"[{cid}] отправил: {text[:60]}")
            await asyncio.sleep(random.uniform(0.5, 1.5))
        st.last_bot_reply_time = time.time()
        memory.mark_replied(tmid)
        memory.save()
    finally:
        st.force_reply_to = None
        st.in_progress = False


# ==================== МЕНЮ БОТА ====================
def main_menu():
    kb = [
        [KeyboardButton(text="📋 Сессии"), KeyboardButton(text="📊 Статус")],
        [KeyboardButton(text="🚪 Войти в чат"), KeyboardButton(text="📢 Войти в канал")],
        [KeyboardButton(text="🚪 Выйти из чата"), KeyboardButton(text="🎙 Войти в войс")],
        [KeyboardButton(text="🎙 Войти на N сек"), KeyboardButton(text="🚪 Выйти из войса")],
        [KeyboardButton(text="🔊 MP3 в войс"), KeyboardButton(text="⏱ МП3 на N сек")],
        [KeyboardButton(text="💬 Написать"), KeyboardButton(text="✉️ ЛС юзеру")],
        [KeyboardButton(text="🖼 Аватар"), KeyboardButton(text="✏️ Имя")],
        [KeyboardButton(text="🎭 Реакция"), KeyboardButton(text="👥 Участники")],
        [KeyboardButton(text="⚙️ Модели"), KeyboardButton(text="🔇 ИИ вкл/выкл")],
        [KeyboardButton(text="🔄 Перезагрузить"), KeyboardButton(text="📜 Логи")],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


def sessions_kb(action, suffix="all"):
    rows, row = [], []
    for sid in list_sids():
        name = CLIENT_META[sid]["name"]
        row.append(InlineKeyboardButton(text=name[:20], callback_data=f"{action}:{sid}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    if suffix == "all":
        rows.append([InlineKeyboardButton(text="✅ ВСЕ СЕССИИ", callback_data=f"{action}:__all__")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


if dp:
    @dp.message(Command("start"))
    async def cmd_start(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        await m.answer(f"Панель. Сессий онлайн: {len(CLIENTS)}", reply_markup=main_menu())

    @dp.message(F.text == "📋 Сессии")
    async def sess_list(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        if not CLIENTS: await m.answer("пусто"); return
        lines = [f"• {CLIENT_META[s]['name']} ({CLIENT_META[s]['id']})" for s in list_sids()]
        await m.answer("Сессии:\n" + "\n".join(lines))

    @dp.message(F.text == "📊 Статус")
    async def stat(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        await m.answer(f"онлайн: {len(CLIENTS)}\nИИ: {'вкл' if SETTINGS.get('ai_enabled') else 'выкл'}\nмоделей: {len(MODELS)}")

    @dp.message(F.text == "🚪 Войти в чат")
    async def jc(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        ADMIN_STATE[m.from_user.id] = {"a": "join_chat"}
        await m.answer("Кинь ссылку/юзернейм/инвайт:")

    @dp.message(F.text == "📢 Войти в канал")
    async def jch(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        ADMIN_STATE[m.from_user.id] = {"a": "join_channel"}
        await m.answer("Кинь ссылку/юзернейм канала:")

    @dp.message(F.text == "🚪 Выйти из чата")
    async def lc(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        ADMIN_STATE[m.from_user.id] = {"a": "leave"}
        await m.answer("Кинь ссылку/юзернейм для выхода:")

    @dp.message(F.text == "🎙 Войти в войс")
    async def vj(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        if not PYTGCALLS_OK:
            await m.answer(f"pytgcalls не загружен: {PYTG_ERR}"); return
        ADMIN_STATE[m.from_user.id] = {"a": "voice_join"}
        await m.answer("Кинь ссылку/юзернейм чата с войсом:")

    @dp.message(F.text == "🎙 Войти на N сек")
    async def vjn(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        if not PYTGCALLS_OK:
            await m.answer(f"pytgcalls не загружен: {PYTG_ERR}"); return
        ADMIN_STATE[m.from_user.id] = {"a": "voice_timed_link"}
        await m.answer("Кинь ссылку/юзернейм чата с войсом:")

    @dp.message(F.text == "⏱ МП3 на N сек")
    async def mp3n(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        if not PYTGCALLS_OK:
            await m.answer(f"pytgcalls не загружен: {PYTG_ERR}"); return
        files = list_voice_files()
        rows = []
        for fname in files[:12]:
            p = os.path.join(DATA_DIR, "voice", fname)
            try: sz = os.path.getsize(p) // 1024
            except Exception: sz = 0
            rows.append([InlineKeyboardButton(
                text=f"🎵 {fname[:35]} ({sz}K)",
                callback_data=f"vpick_timed:{fname}")])
        rows.append([InlineKeyboardButton(text="📥 Загрузить новую",
                                          callback_data="vpick_timed:__upload__")])
        vol = "ГРОМКО 🔥" if VOLUME_LOUD.get("on") else "обычная"
        rows.append([InlineKeyboardButton(text=f"🔊 Громкость: {vol}",
                                          callback_data="vpick_timed:__toggle_vol__")])
        await m.answer("Выбери mp3:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    @dp.message(F.text == "🚪 Выйти из войса")
    async def vl(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        ADMIN_STATE[m.from_user.id] = {"a": "voice_leave"}
        await m.answer("Кинь ссылку/юзернейм чата:")

    @dp.message(F.text == "🔊 MP3 в войс")
    async def mp3(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        if not PYTGCALLS_OK:
            await m.answer(f"pytgcalls не загружен: {PYTG_ERR}"); return
        files = list_voice_files()
        rows = []
        for fname in files[:12]:
            p = os.path.join(DATA_DIR, "voice", fname)
            try: sz = os.path.getsize(p) // 1024
            except Exception: sz = 0
            rows.append([InlineKeyboardButton(
                text=f"🎵 {fname[:35]} ({sz}K)",
                callback_data=f"vpick_play:{fname}")])
        rows.append([InlineKeyboardButton(text="📥 Загрузить новую",
                                          callback_data="vpick_play:__upload__")])
        vol = "ГРОМКО 🔥" if VOLUME_LOUD.get("on") else "обычная"
        rows.append([InlineKeyboardButton(text=f"🔊 Громкость: {vol}",
                                          callback_data="vpick_play:__toggle_vol__")])
        await m.answer("Выбери mp3:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    @dp.message(F.text == "🎬 Медиа в войс")
    async def mv(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        if not PYTGCALLS_OK:
            await m.answer(f"pytgcalls: {PYTG_ERR}"); return
        files = list_media_files()
        rows = []
        for fname in files[:12]:
            p = os.path.join(MEDIA_DIR, fname)
            try: sz = os.path.getsize(p) // 1024
            except Exception: sz = 0
            k = media_kind(p)
            icon = {"image": "🖼", "video": "🎥", "gif": "🎞", "tgs": "🌟"}.get(k, "📄")
            rows.append([InlineKeyboardButton(
                text=f"{icon} {fname[:33]} ({sz}K)",
                callback_data=f"mpick_play:{fname}")])
        rows.append([InlineKeyboardButton(text="📥 Загрузить новое",
                                          callback_data="mpick_play:__upload__")])
        await m.answer("Выбери медиа:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    @dp.message(F.text == "🎬 Медиа на N сек")
    async def mvn(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        if not PYTGCALLS_OK:
            await m.answer(f"pytgcalls: {PYTG_ERR}"); return
        files = list_media_files()
        rows = []
        for fname in files[:12]:
            p = os.path.join(MEDIA_DIR, fname)
            try: sz = os.path.getsize(p) // 1024
            except Exception: sz = 0
            k = media_kind(p)
            icon = {"image": "🖼", "video": "🎥", "gif": "🎞", "tgs": "🌟"}.get(k, "📄")
            rows.append([InlineKeyboardButton(
                text=f"{icon} {fname[:33]} ({sz}K)",
                callback_data=f"mpick_timed:{fname}")])
        rows.append([InlineKeyboardButton(text="📥 Загрузить новое",
                                          callback_data="mpick_timed:__upload__")])
        await m.answer("Выбери медиа:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    @dp.message(F.text == "💬 Написать")
    async def wr(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        ADMIN_STATE[m.from_user.id] = {"a": "write_pick"}
        await m.answer("Выбери сессию:", reply_markup=sessions_kb("wr", "all"))

    @dp.message(F.text == "✉️ ЛС юзеру")
    async def dm(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        ADMIN_STATE[m.from_user.id] = {"a": "dm_pick"}
        await m.answer("Выбери сессию:", reply_markup=sessions_kb("dm", "all"))

    @dp.message(F.text == "🖼 Аватар")
    async def av(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        ADMIN_STATE[m.from_user.id] = {"a": "ava_pick"}
        await m.answer("Выбери сессию:", reply_markup=sessions_kb("ava", "none"))

    @dp.message(F.text == "✏️ Имя")
    async def rn(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        ADMIN_STATE[m.from_user.id] = {"a": "rnm_pick"}
        await m.answer("Выбери сессию:", reply_markup=sessions_kb("rnm", "none"))

    @dp.message(F.text == "🎭 Реакция")
    async def reac(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        ADMIN_STATE[m.from_user.id] = {"a": "react_link"}
        await m.answer("Кинь ссылку на сообщение (t.me/chat/123):")

    @dp.message(F.text == "👥 Участники")
    async def parts(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        ADMIN_STATE[m.from_user.id] = {"a": "parts_link"}
        await m.answer("Кинь ссылку/юзернейм чата:")

    @dp.message(F.text == "⚙️ Модели")
    async def mm(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        lines = [f"{i+1}. {x}" for i, x in enumerate(MODELS)]
        await m.answer("Модели:\n" + "\n".join(lines) + "\n\nКоманды:\n/add_model <имя>\n/del_model <номер>\n/models_up <номер> (наверх)")

    @dp.message(F.text == "🔇 ИИ вкл/выкл")
    async def toggle(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        SETTINGS["ai_enabled"] = not SETTINGS.get("ai_enabled", True)
        save_json(SETTINGS_FILE, SETTINGS)
        await m.answer(f"ИИ: {'вкл' if SETTINGS['ai_enabled'] else 'выкл'}")

    @dp.message(F.text == "🔄 Перезагрузить")
    async def rel(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        for c in CLIENTS.values():
            try: await c.disconnect()
            except: pass
        CLIENTS.clear(); CLIENT_META.clear()
        await load_env_sessions()
        await m.answer(f"перезагружено: {len(CLIENTS)}")

    @dp.message(F.text == "📜 Логи")
    async def lg(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        try:
            with open("/proc/1/fd/1") as f: data = f.read()[-2500:]
            await m.answer("```\n" + data + "\n```", parse_mode="Markdown")
        except Exception as e:
            await m.answer(f"не вышло: {e}")

    @dp.message(Command("grab"))
    async def cmd_grab(m: at.Message):
        """Скачивает последнее медиа от ADMIN_ID в диалогах vortex."""
        if m.from_user.id != ADMIN_ID: return
        c = CLIENTS.get(AI_SESSION_ID)
        if not c:
            await m.answer(f"vortex ({AI_SESSION_ID}) не онлайн"); return
        await m.answer("ищу последнее медиа от тебя...")
        try:
            found = None

            def is_wanted(msg):
                if getattr(msg, "voice", None): return False
                if getattr(msg, "video_note", None): return False
                if getattr(msg, "web_preview", None): return False
                if getattr(msg, "audio", None): return False
                return bool(msg.video or msg.photo or msg.document
                            or msg.animation or msg.sticker)

            # 1) диалог напрямую с ADMIN_ID
            try:
                async for msg in c.iter_messages(ADMIN_ID, limit=30):
                    if is_wanted(msg):
                        found = msg; break
            except Exception as e:
                log.warning(f"grab admin dm: {e}")

            # 2) по всем диалогам, только from_user=ADMIN_ID
            if not found:
                async for dialog in c.iter_dialogs(limit=30):
                    if not dialog.is_user: continue
                    try:
                        async for msg in c.iter_messages(dialog.id, limit=15, from_user=ADMIN_ID):
                            if is_wanted(msg):
                                found = msg; break
                    except Exception:
                        continue
                    if found: break

            if not found:
                await m.answer("не нашёл медиа от тебя.\n"
                               "Кинь файл vortex в лс и напиши /grab"); return

            path = await found.download_media(file=MEDIA_DIR)
            if not path:
                await m.answer("не смог скачать"); return

            base = os.path.basename(path)
            if "." not in base or base.endswith("."):
                if found.video: ext = ".mp4"
                elif found.photo: ext = ".jpg"
                elif found.animation: ext = ".mp4"
                elif found.sticker:
                    ext = ".webm" if getattr(found.sticker, "is_video", False) else ".webp"
                else: ext = ".bin"
                newpath = os.path.join(MEDIA_DIR, base + ext)
                try:
                    os.rename(path, newpath); path = newpath
                except Exception: pass

            sz = os.path.getsize(path)
            kind = media_kind(path)
            await m.answer(f"✓ сохранено: {os.path.basename(path)} ({kind}, {sz // 1024}K)")

        except Exception as e:
            await m.answer(f"ошибка: {e}")

    @dp.message(Command("files"))
    async def cmd_files(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        vd = os.path.join(DATA_DIR, "voice")
        if not os.path.exists(vd):
            await m.answer("пусто"); return
        fs = sorted(os.listdir(vd))
        if not fs:
            await m.answer("пусто"); return
        lines = []
        for f in fs[-30:]:
            p = os.path.join(vd, f)
            sz = os.path.getsize(p) // 1024
            lines.append(f"• {f} ({sz} KB)")
        await m.answer("Сохранённые mp3 (последние 30):\n" + "\n".join(lines))

    @dp.message(Command("add_model"))
    async def addm(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        parts = m.text.split(maxsplit=1)
        if len(parts) < 2: await m.answer("usage: /add_model <id>"); return
        MODELS.append(parts[1].strip()); save_json(MODELS_FILE, MODELS)
        await m.answer(f"добавлено: {parts[1]}")

    @dp.message(Command("del_model"))
    async def delm(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        try:
            i = int(m.text.split()[1]) - 1
            rm = MODELS.pop(i); save_json(MODELS_FILE, MODELS)
            await m.answer(f"удалено: {rm}")
        except Exception as e:
            await m.answer(f"ошибка: {e}")

    @dp.message(F.document | F.audio | F.voice | F.photo | F.video | F.animation | F.sticker)
    async def doc(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        st = ADMIN_STATE.get(m.from_user.id, {})
        # === универсальный приём медиа ===
        if st.get("a") in ("media_upload", "media_timed_upload"):
            file_obj = m.document or m.audio or m.voice or (m.photo[-1] if m.photo else None) or m.video or m.animation or m.sticker
            if not file_obj:
                await m.answer("не понял что пришло"); return
            fname = "media.bin"
            if hasattr(file_obj, "file_name") and file_obj.file_name:
                fname = file_obj.file_name
            elif m.photo:
                fname = "photo.jpg"
            elif m.video:
                fname = "video.mp4"
            elif m.animation:
                fname = "anim.mp4"
            elif m.sticker:
                if getattr(file_obj, "is_video", False):
                    fname = "sticker.webm"
                elif getattr(file_obj, "is_animated", False):
                    fname = "sticker.tgs"
                else:
                    fname = "sticker.webp"
            safe = re.sub(r"[^A-Za-z0-9._\-]", "_", fname)
            ts = int(time.time())
            path = os.path.join(MEDIA_DIR, f"{ts}_{safe}")
            try:
                await bot.download(file_obj, destination=path)
            except Exception as e:
                await m.answer(f"не смог скачать: {e}"); return
            sz = os.path.getsize(path)
            if sz < 200:
                await m.answer(f"файл слишком маленький: {sz}"); return
            kind = media_kind(path)
            if kind == "unknown":
                await m.answer(f"формат не распознан: {safe}"); return
            if kind == "tgs":
                await m.answer("tgs не поддерживается"); return
            st["media"] = path
            st["a"] = "media_link" if st.get("a") == "media_upload" else "media_timed_link"
            ADMIN_STATE[m.from_user.id] = st
            await m.answer(f"✓ сохранено ({kind}, {sz}). Кинь ссылку на чат:")
            return

        # определяем файл: document / audio / voice
        file_obj = m.document or m.audio or m.voice
        if file_obj is None:
            return
        # имя файла: сохраняем каждую мп3 отдельно в voice/
        voice_dir = os.path.join(DATA_DIR, "voice")
        os.makedirs(voice_dir, exist_ok=True)

        orig = "voice_file"
        if getattr(file_obj, "file_name", None):
            orig = file_obj.file_name
        # безопасное имя: только буквы/цифры/._-
        safe = re.sub(r"[^A-Za-z0-9._\-]", "_", orig)
        ts = int(time.time())
        fname = f"{ts}_{safe}"
        path = os.path.join(voice_dir, fname)
        try:
            await bot.download(file_obj, destination=path)
        except Exception as e:
            await m.answer(f"не смог скачать: {e}"); return
        sz = os.path.getsize(path)
        if sz < 500:
            await m.answer(f"файл слишком маленький: {sz} байт"); return
        if st.get("a") == "mp3_upload":
            st["mp3"] = path
            st["a"] = "voice_link_mp3"
            ADMIN_STATE[m.from_user.id] = st
            await m.answer(f"mp3 сохранён ({sz} байт). Теперь кинь ссылку чата с войсом:")
        elif st.get("a") == "mp3_timed_upload":
            st["mp3"] = path
            st["a"] = "mp3_timed_link"
            ADMIN_STATE[m.from_user.id] = st
            await m.answer(f"mp3 сохранён ({sz} байт). Теперь кинь ссылку чата:")
        else:
            await m.answer("сначала нажми '🔊 MP3 в войс' или '⏱ МП3 на N сек'")

    @dp.callback_query()
    async def cb(cbq: at.CallbackQuery):
        if cbq.from_user.id != ADMIN_ID: await cbq.answer(); return

        # === обработка кнопок медиа ===
        if cbq.data.startswith("mpick_play:") or cbq.data.startswith("mpick_timed:"):
            mode = "play" if cbq.data.startswith("mpick_play:") else "timed"
            val = cbq.data.split(":", 1)[1]
            if val == "__upload__":
                a = "media_upload" if mode == "play" else "media_timed_upload"
                ADMIN_STATE[cbq.from_user.id] = {"a": a}
                await cbq.message.answer("Кинь медиа (фото/видео/gif/стикер).\n⚠️ Если файл > 20 МБ — перешли его vortex в ЛС и напиши /grab")
            else:
                path = os.path.join(MEDIA_DIR, val)
                if not os.path.exists(path):
                    await cbq.message.answer("файл пропал")
                    await cbq.answer()
                    return
                a = "media_link" if mode == "play" else "media_timed_link"
                ADMIN_STATE[cbq.from_user.id] = {"a": a, "media": path}
                await cbq.message.answer(f"выбрано: {val}\nКинь ссылку/юзернейм чата:")
            await cbq.answer()
            return

        # выбор mp3 из списка
        if cbq.data.startswith("vpick_play:") or cbq.data.startswith("vpick_timed:"):
            mode = "play" if cbq.data.startswith("vpick_play:") else "timed"
            val = cbq.data.split(":", 1)[1]
            if val == "__upload__":
                a = "mp3_upload" if mode == "play" else "mp3_timed_upload"
                ADMIN_STATE[cbq.from_user.id] = {"a": a}
                await cbq.message.answer("Кинь mp3/ogg файл:")
            elif val == "__toggle_vol__":
                VOLUME_LOUD["on"] = not VOLUME_LOUD.get("on", False)
                SETTINGS["volume_loud"] = VOLUME_LOUD["on"]
                save_json(SETTINGS_FILE, SETTINGS)
                await cbq.message.answer(
                    f"громкость: {'ГРОМКО 🔥' if VOLUME_LOUD['on'] else 'обычная'}")
            else:
                path = os.path.join(DATA_DIR, "voice", val)
                if not os.path.exists(path):
                    await cbq.message.answer("файл пропал"); await cbq.answer(); return
                a = "voice_link_mp3" if mode == "play" else "mp3_timed_link"
                ADMIN_STATE[cbq.from_user.id] = {"a": a, "mp3": path}
                await cbq.message.answer(f"выбрано: {val}\nКинь ссылку/юзернейм чата:")
            await cbq.answer()
            return

        act, sid = cbq.data.split(":", 1)
        st = ADMIN_STATE.get(cbq.from_user.id, {})
        sids = list_sids() if sid == "__all__" else [sid]

        if act == "wr":
            st.update({"a": "write_text", "sids": sids})
            ADMIN_STATE[cbq.from_user.id] = st
            await cbq.message.answer("Напиши текст:")
        elif act == "dm":
            st.update({"a": "dm_username", "sids": sids})
            ADMIN_STATE[cbq.from_user.id] = st
            await cbq.message.answer("Кинь @юзернейм или id:")
        elif act == "ava":
            st.update({"a": "ava_photo", "sid": sid})
            ADMIN_STATE[cbq.from_user.id] = st
            await cbq.message.answer("Кинь фото:")
        elif act == "rnm":
            st.update({"a": "rnm_name", "sid": sid})
            ADMIN_STATE[cbq.from_user.id] = st
            await cbq.message.answer("Кинь новое имя:")
        elif act == "vj":
            st.update({"a": "voice_join_do", "sids": sids})
            ADMIN_STATE[cbq.from_user.id] = st
            link = st.get("link")
            await cbq.message.answer(f"Захожу {len(sids)} сессиями в войс {link}...")
            await do_voice_join(cbq.from_user.id, link, sids)
        elif act == "vl":
            link = st.get("link")
            await do_voice_leave(cbq.from_user.id, link, sids)
        elif act == "mp3go":
            link = st.get("link")
            mp3p = st.get("mp3")
            await do_voice_play(cbq.from_user.id, link, sids, mp3p)
        elif act == "media_play":
            link = st.get("link")
            path = st.get("media")
            await do_media_play(cbq.from_user.id, link, sids, path, timed_secs=None)
        elif act == "media_timed_play":
            link = st.get("link")
            path = st.get("media")
            secs = st.get("secs", 60)
            await do_media_play(cbq.from_user.id, link, sids, path, timed_secs=secs)
        elif act == "vj_timed":
            link = st.get("link")
            secs = st.get("secs", 60)
            await do_voice_join_timed(cbq.from_user.id, link, sids, secs)
        elif act == "mp3_timed":
            link = st.get("link")
            secs = st.get("secs", 60)
            mp3p = st.get("mp3")
            await do_voice_play_timed(cbq.from_user.id, link, sids, mp3p, secs)
        elif act == "jc":
            await do_join(cbq.from_user.id, st.get("link"), sids, is_channel=False)
        elif act == "jch":
            await do_join(cbq.from_user.id, st.get("link"), sids, is_channel=True)
        elif act == "lc":
            await do_leave(cbq.from_user.id, st.get("link"), sids)
        await cbq.answer()

    @dp.message()
    async def text(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        st = ADMIN_STATE.get(m.from_user.id)
        if not st: return
        a = st.get("a")

        if a in ("join_chat", "join_channel"):
            st["link"] = m.text.strip()
            suffix = "all"
            await m.answer("Выбери сессии:", reply_markup=sessions_kb("jc" if a=="join_chat" else "jch", suffix))

        elif a == "leave":
            st["link"] = m.text.strip()
            await m.answer("Выбери сессии:", reply_markup=sessions_kb("lc", "all"))

        elif a == "voice_join":
            st["link"] = m.text.strip()
            await m.answer("Выбери сессии:", reply_markup=sessions_kb("vj", "all"))

        elif a == "voice_link_mp3":
            st["link"] = m.text.strip()
            await m.answer("Выбери сессии:", reply_markup=sessions_kb("mp3go", "all"))

        elif a == "media_link":
            st["link"] = m.text.strip()
            st["a"] = "media_sessions"
            ADMIN_STATE[m.from_user.id] = st
            await m.answer("Выбери сессии:", reply_markup=sessions_kb("media_play", "all"))

        elif a == "media_timed_link":
            st["link"] = m.text.strip()
            st["a"] = "media_timed_secs"
            ADMIN_STATE[m.from_user.id] = st
            await m.answer("Сколько секунд играть?")

        elif a == "media_timed_secs":
            try:
                st["secs"] = int(m.text.strip())
            except ValueError:
                await m.answer("нужно число"); return
            st["a"] = "media_timed_sessions"
            ADMIN_STATE[m.from_user.id] = st
            await m.answer(f"ок, {st['secs']} сек. Выбери сессии:",
                           reply_markup=sessions_kb("media_timed_play", "all"))

        elif a == "voice_timed_link":
            st["link"] = m.text.strip()
            st["a"] = "voice_timed_secs"
            ADMIN_STATE[m.from_user.id] = st
            await m.answer("Сколько секунд сидеть? (например 60)")

        elif a == "voice_timed_secs":
            try:
                st["secs"] = int(m.text.strip())
            except ValueError:
                await m.answer("нужно число, попробуй снова"); return
            st["a"] = "voice_timed_pick"
            ADMIN_STATE[m.from_user.id] = st
            await m.answer(f"ок, {st['secs']} сек. Выбери сессии:",
                           reply_markup=sessions_kb("vj_timed", "all"))

        elif a == "mp3_timed_upload":
            pass  # ждём документ

        elif a == "mp3_timed_link":
            st["link"] = m.text.strip()
            st["a"] = "mp3_timed_secs"
            ADMIN_STATE[m.from_user.id] = st
            await m.answer("Сколько секунд играть?")

        elif a == "mp3_timed_secs":
            try:
                st["secs"] = int(m.text.strip())
            except ValueError:
                await m.answer("нужно число"); return
            st["a"] = "mp3_timed_pick"
            ADMIN_STATE[m.from_user.id] = st
            await m.answer(f"ок, {st['secs']} сек. Выбери сессии:",
                           reply_markup=sessions_kb("mp3_timed", "all"))

        elif a == "voice_leave":
            st["link"] = m.text.strip()
            await m.answer("Выбери сессии:", reply_markup=sessions_kb("vl", "all"))

        elif a == "write_text":
            st["text"] = m.text
            st["a"] = "write_link"
            ADMIN_STATE[m.from_user.id] = st
            await m.answer("Кинь ссылку/юзернейм чата:")

        elif a == "write_link":
            link = m.text.strip()
            txt = st.get("text")
            for sid in st.get("sids", []):
                c = CLIENTS.get(sid)
                if not c: continue
                e = await resolve_entity(c, link)
                if not e: continue
                try:
                    await c.send_message(e, txt)
                    await m.answer(f"✓ {CLIENT_META[sid]['name']}")
                except Exception as ex:
                    await m.answer(f"✗ {CLIENT_META[sid]['name']}: {ex}")

        elif a == "dm_username":
            st["target"] = m.text.strip()
            st["a"] = "dm_text"
            ADMIN_STATE[m.from_user.id] = st
            await m.answer("Текст ЛС:")

        elif a == "dm_text":
            txt = m.text
            tgt = st.get("target")
            for sid in st.get("sids", []):
                c = CLIENTS.get(sid)
                if not c: continue
                try:
                    e = await c.get_entity(tgt)
                    await c.send_message(e, txt)
                    await m.answer(f"✓ {CLIENT_META[sid]['name']}")
                except Exception as ex:
                    await m.answer(f"✗ {CLIENT_META[sid]['name']}: {ex}")

        elif a == "rnm_name":
            sid = st.get("sid")
            c = CLIENTS.get(sid)
            if c:
                try:
                    await c(UpdateProfileRequest(first_name=m.text.strip()))
                    await m.answer(f"✓ {sid} имя обновлено")
                except Exception as ex:
                    await m.answer(f"✗ {ex}")

        elif a == "react_link":
            st["link"] = m.text.strip()
            st["a"] = "react_emoji"
            ADMIN_STATE[m.from_user.id] = st
            await m.answer("Кинь эмодзи (например 🔥):")

        elif a == "react_emoji":
            st["emoji"] = m.text.strip()
            link = st["link"]
            m2 = re.match(r"(?:https?://)?t\.me/([A-Za-z0-9_]+)/(\d+)", link)
            if not m2:
                await m.answer("не понял ссылку"); return
            uname, mid = m2.group(1), int(m2.group(2))
            for sid in list_sids():
                c = CLIENTS[sid]
                try:
                    e = await c.get_entity(uname)
                    await c(SendReactionRequest(peer=e, msg_id=mid,
                        reaction=[types.ReactionEmoji(emoticon=st["emoji"])]))
                    await m.answer(f"✓ {CLIENT_META[sid]['name']}")
                except Exception as ex:
                    await m.answer(f"✗ {CLIENT_META[sid]['name']}: {ex}")

        elif a == "parts_link":
            link = m.text.strip()
            for sid in list_sids()[:1]:
                c = CLIENTS[sid]
                e = await resolve_entity(c, link)
                if not e: continue
                lines = []
                try:
                    async for u in c.iter_participants(e, limit=50):
                        nm = u.first_name or u.username or str(u.id)
                        lines.append(f"• {nm} (@{u.username or '-'}) id={u.id}")
                except Exception as ex:
                    await m.answer(f"ошибка: {ex}"); return
                await m.answer("Участники:\n" + "\n".join(lines))

        elif a == "ava_photo":
            await m.answer("жду фото (отправь именно фото, не файлом)")

    @dp.message(F.photo)
    async def photo(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        st = ADMIN_STATE.get(m.from_user.id, {})
        if st.get("a") == "ava_photo":
            sid = st.get("sid")
            c = CLIENTS.get(sid)
            if not c: await m.answer("нет сессии"); return
            path = os.path.join(DATA_DIR, "ava.jpg")
            await bot.download(m.photo[-1], destination=path)
            try:
                f = await c.upload_file(path)
                await c(UploadProfilePhotoRequest(file=f))
                await m.answer(f"✓ аватар обновлён: {sid}")
            except Exception as ex:
                await m.answer(f"✗ {ex}")


async def do_join(uid, link, sids, is_channel=False):
    for sid in sids:
        c = CLIENTS.get(sid)
        if not c: continue
        e = await resolve_entity(c, link)
        if not e:
            await bot.send_message(uid, f"✗ {CLIENT_META[sid]['name']}: не смог найти")
            continue
        try:
            if is_channel:
                await c(JoinChannelRequest(e))
            else:
                try: await c(JoinChannelRequest(e))
                except: pass
            await bot.send_message(uid, f"✓ {CLIENT_META[sid]['name']}")
        except Exception as ex:
            await bot.send_message(uid, f"✗ {CLIENT_META[sid]['name']}: {ex}")
        await asyncio.sleep(random.uniform(1.5, 3.0))


async def do_leave(uid, link, sids):
    for sid in sids:
        c = CLIENTS.get(sid)
        if not c: continue
        e = await resolve_entity(c, link)
        if not e: continue
        try:
            await c(LeaveChannelRequest(e))
            await bot.send_message(uid, f"✓ вышел {CLIENT_META[sid]['name']}")
        except Exception as ex:
            await bot.send_message(uid, f"✗ {CLIENT_META[sid]['name']}: {ex}")
        await asyncio.sleep(random.uniform(1.5, 3.0))


async def do_voice_join(uid, link, sids):
    if not PYTGCALLS_OK:
        await bot.send_message(uid, "pytgcalls не установлен"); return
    for sid in sids:
        c = CLIENTS.get(sid)
        if not c: continue
        e = await resolve_entity(c, link)
        if not e:
            await bot.send_message(uid, f"✗ {CLIENT_META[sid]['name']}: чат не найден")
            continue
        try:
            await c(JoinChannelRequest(e))
        except: pass
        try:
            py = VOICE_CALLS.get(sid)
            if not py:
                py = PyTgCalls(c)
                await py.start()
                VOICE_CALLS[sid] = py
            try:
                await py.play(utils.get_peer_id(e), MediaStream(os.path.join(DATA_DIR, "silent.ogg")))
            except: pass
            await bot.send_message(uid, f"✓ {CLIENT_META[sid]['name']} вошёл в войс")
        except Exception as ex:
            await bot.send_message(uid, f"✗ {CLIENT_META[sid]['name']}: {ex}")
        await asyncio.sleep(1.0)


async def do_voice_leave(uid, link, sids):
    for sid in sids:
        c = CLIENTS.get(sid)
        py = VOICE_CALLS.get(sid)
        if not c or not py: continue
        e = await resolve_entity(c, link)
        if not e: continue
        try:
            await py.leave_call(utils.get_peer_id(e))
            await bot.send_message(uid, f"✓ {CLIENT_META[sid]['name']} вышел из войса")
        except Exception as ex:
            await bot.send_message(uid, f"✗ {CLIENT_META[sid]['name']}: {ex}")


async def do_voice_join_timed(uid, link, sids, secs):
    """Заходят в войс с silent.ogg на secs секунд, потом выходят."""
    if not PYTGCALLS_OK:
        await bot.send_message(uid, "pytgcalls не установлен"); return
    if not os.path.exists(SILENT_OGG):
        await bot.send_message(uid, "silent.ogg не сгенерирован, нет ffmpeg"); return
    entered = []
    for sid in sids:
        c = CLIENTS.get(sid)
        if not c: continue
        e = await resolve_entity(c, link)
        if not e:
            await bot.send_message(uid, f"✗ {CLIENT_META[sid]['name']}: не найден")
            continue
        try:
            await c(JoinChannelRequest(e))
        except: pass
        try:
            py = VOICE_CALLS.get(sid)
            if not py:
                py = PyTgCalls(c)
                await py.start()
                VOICE_CALLS[sid] = py
            try:
                await py.play(utils.get_peer_id(e), MediaStream(SILENT_OGG))
            except Exception as e2:
                await bot.send_message(uid, f"✗ play {CLIENT_META[sid]['name']}: {e2}")
                continue
            entered.append((sid, e))
            await bot.send_message(uid, f"✓ {CLIENT_META[sid]['name']} зашёл в войс на {secs}с")
        except Exception as ex:
            await bot.send_message(uid, f"✗ {CLIENT_META[sid]['name']}: {ex}")
        await asyncio.sleep(1.0)

    if not entered:
        return
    await bot.send_message(uid, f"сидят... выйдут через {secs} сек")
    await asyncio.sleep(secs)
    for sid, e in entered:
        try:
            py = VOICE_CALLS.get(sid)
            if py:
                await py.leave_call(utils.get_peer_id(e))
            await bot.send_message(uid, f"↩ {CLIENT_META[sid]['name']} вышел")
        except Exception as ex:
            await bot.send_message(uid, f"↩ {CLIENT_META[sid]['name']}: {ex}")


async def do_voice_play_timed(uid, link, sids, mp3, secs):
    """Играют mp3 в войсе secs секунд, потом выходят."""
    if not PYTGCALLS_OK:
        await bot.send_message(uid, "pytgcalls не установлен"); return
    if not mp3 or not os.path.exists(mp3):
        await bot.send_message(uid, "файл не найден"); return
    entered = []
    for sid in sids:
        c = CLIENTS.get(sid)
        if not c: continue
        e = await resolve_entity(c, link)
        if not e:
            await bot.send_message(uid, f"✗ {CLIENT_META[sid]['name']}: не найден")
            continue
        try:
            await c(JoinChannelRequest(e))
        except: pass
        try:
            py = VOICE_CALLS.get(sid)
            if not py:
                py = PyTgCalls(c)
                await py.start()
                VOICE_CALLS[sid] = py
            await py.play(utils.get_peer_id(e), MediaStream(mp3))
            entered.append((sid, e))
            await bot.send_message(uid, f"▶ {CLIENT_META[sid]['name']} играет {secs}с")
        except Exception as ex:
            await bot.send_message(uid, f"✗ {CLIENT_META[sid]['name']}: {ex}")
        await asyncio.sleep(0.1)

    if not entered:
        return
    await asyncio.sleep(secs)
    for sid, e in entered:
        try:
            py = VOICE_CALLS.get(sid)
            if py:
                await py.leave_call(utils.get_peer_id(e))
            await bot.send_message(uid, f"↩ {CLIENT_META[sid]['name']} вышел")
        except Exception as ex:
            await bot.send_message(uid, f"↩ {CLIENT_META[sid]['name']}: {ex}")


async def do_media_play(uid, link, sids, media_path, timed_secs=None):
    if not PYTGCALLS_OK:
        await bot.send_message(uid, "pytgcalls не установлен"); return
    if not media_path or not os.path.exists(media_path):
        await bot.send_message(uid, "файл не найден"); return
    kind = media_kind(media_path)
    if kind == "tgs":
        await bot.send_message(uid, "tgs не поддерживается"); return
    await bot.send_message(uid, f"конвертирую ({kind})...")
    mp4 = await convert_media(media_path)
    if not mp4:
        await bot.send_message(uid, "не удалось конвертнуть (см. логи Railway)"); return
    dur = mp3_duration(mp4)
    if dur <= 0: dur = timed_secs if timed_secs else 30
    entered = []

    async def get_call_and_entity(sid):
        c = CLIENTS.get(sid)
        if not c: return None
        e = await resolve_entity(c, link)
        if not e:
            await bot.send_message(uid, f"✗ {CLIENT_META[sid]['name']}: не найден")
            return None
        try:
            await c(JoinChannelRequest(e))
        except: pass
        try:
            py = VOICE_CALLS.get(sid)
            if not py:
                py = PyTgCalls(c); await py.start(); VOICE_CALLS[sid] = py
            return (sid, e, py)
        except Exception as ex:
            await bot.send_message(uid, f"✗ {CLIENT_META[sid]['name']}: {ex}")
            return None

    async def play_one(sid, e, py):
        try:
            await py.play(utils.get_peer_id(e), MediaStream(mp4))
            entered.append((sid, e, time.time()))
            await bot.send_message(uid, f"▶ {CLIENT_META[sid]['name']}")
        except Exception as ex:
            await bot.send_message(uid, f"✗ play {CLIENT_META[sid]['name']}: {ex}")

    half = max(1, len(sids) // 2)
    first = sids[:half]; rest = sids[half:]
    joined_first = []
    for sid in first:
        r = await get_call_and_entity(sid)
        if r: joined_first.append(r)
        await asyncio.sleep(0.25)
    if not joined_first:
        await bot.send_message(uid, "никто не зашёл"); return

    rest_task = None
    n = len(joined_first)

    async def run_rest():
        await asyncio.sleep(0.25)
        tasks = []
        for sid in rest:
            async def one(s=sid):
                r = await get_call_and_entity(s)
                if not r: return
                s2, e2, py2 = r
                await play_one(s2, e2, py2)
            tasks.append(asyncio.create_task(one()))
        await asyncio.gather(*tasks)

    for i, (sid, e, py) in enumerate(joined_first):
        if i == n - 2 and rest:
            rest_task = asyncio.create_task(run_rest())
        await play_one(sid, e, py)
        await asyncio.sleep(0.25)
    if rest_task: await rest_task
    if not entered: return
    if timed_secs:
        await bot.send_message(uid, f"играют {timed_secs}с, потом выйдут")

        async def leave_after(sid, e, start_ts):
            elapsed = time.time() - start_ts
            wait = max(0.0, timed_secs - elapsed)
            await asyncio.sleep(wait)
            try:
                py = VOICE_CALLS.get(sid)
                if py:
                    await py.leave_call(utils.get_peer_id(e))
                VOICE_CALLS.pop(sid, None)
                await bot.send_message(uid, f"↩ {CLIENT_META[sid]['name']} вышел")
            except Exception as ex:
                await bot.send_message(uid, f"↩ {CLIENT_META[sid]['name']}: {ex}")

        await asyncio.gather(*[asyncio.create_task(leave_after(*x)) for x in entered])
    else:
        await bot.send_message(
            uid,
            f"▶ играют. Чтобы остановить — жми 🚪 Выйти из войса"
        )
        if dur > 5:
            period = dur - 2

            async def auto_loop(sid, e):
                while True:
                    await asyncio.sleep(period)
                    if sid not in VOICE_CALLS:
                        return
                    try:
                        py = VOICE_CALLS[sid]
                        await py.play(utils.get_peer_id(e), MediaStream(mp4))
                    except Exception:
                        return

            for sid, e, _ in entered:
                asyncio.create_task(auto_loop(sid, e))


async def do_voice_play(uid, link, sids, mp3):
    if not PYTGCALLS_OK:
        await bot.send_message(uid, "pytgcalls не установлен"); return
    for sid in sids:
        c = CLIENTS.get(sid)
        if not c: continue
        e = await resolve_entity(c, link)
        if not e: continue
        try:
            py = VOICE_CALLS.get(sid)
            if not py:
                py = PyTgCalls(c)
                await py.start()
                VOICE_CALLS[sid] = py
            await py.play(utils.get_peer_id(e), MediaStream(mp3))
            await bot.send_message(uid, f"▶ {CLIENT_META[sid]['name']}")
        except Exception as ex:
            await bot.send_message(uid, f"✗ {CLIENT_META[sid]['name']}: {ex}")
        await asyncio.sleep(1.0)


# ==================== MAIN ====================
async def main():
    log.info(f"админ: {ADMIN_ID}, ИИ сессия: {AI_SESSION_ID}")
    await load_env_sessions()
    if not CLIENTS:
        log.error("нет сессий")

    asyncio.create_task(ai_poller())

    if bot:
        log.info("управляющий бот стартует")
        await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
