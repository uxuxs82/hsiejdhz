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
            # фото -> 60с видео с микро-движением (зум + качание), чтобы TG не убивал поток
            vf = (
                "scale=960:960:force_original_aspect_ratio=decrease,"
                "pad=960:960:(ow-iw)/2:(oh-ih)/2,"
                "zoompan=z='1.0+0.0003*on':"
                "x='iw/2-(iw/zoom/2)+2*sin(on/8)':"
                "y='ih/2-(ih/zoom/2)+2*cos(on/10)':"
                "d=1:s=480x480:fps=24,"
                "format=yuv420p"
            )
            cmd = ["ffmpeg", "-y", "-loop", "1", "-i", path, "-t", "60",
                   "-vf", vf, "-an",
                   "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                   "-movflags", "+faststart", "-threads", "1", out]
            ok, err = await asyncio.get_event_loop().run_in_executor(None, _run, cmd, 180)
        else:
            cmd = ["ffmpeg", "-y", "-i", path,
                   "-vf", "scale=480:-2,fps=24",
                   "-an",
                   "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
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

MODELS_FILE = os.path.join(DATA_DIR, "models.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
MEMORY_FILE = os.path.join(DATA_DIR, "memory.json")

# ==== TTS ====
TTS_DIR = os.path.join(DATA_DIR, "tts")
try:
    os.makedirs(TTS_DIR, exist_ok=True)
except Exception:
    TTS_DIR = DATA_DIR

MARVEL_BOT_USERNAME = "MarvelVoiceBot"
# ==== МАФИЯ ====
MAFIA_BOT = "TrueMafiaBot"
MAFIA_GROUP = os.environ.get("MAFIA_GROUP", "")   # можно переопределить в боте
ADMIN_NAME_IN_GAME = os.environ.get("ADMIN_NAME", "")

MAFIA_CFG_FILE = os.path.join(DATA_DIR, "mafia_cfg.json")
MAFIA_CFG = {"group": MAFIA_GROUP, "admin_name": ADMIN_NAME_IN_GAME}

try:
    if os.path.exists(MAFIA_CFG_FILE):
        with open(MAFIA_CFG_FILE, "r", encoding="utf-8") as _f:
            MAFIA_CFG.update(json.load(_f))
except Exception as _e:
    print(f"mafia cfg load: {_e}")


def mafia_save_cfg():
    try:
        with open(MAFIA_CFG_FILE, "w", encoding="utf-8") as _f:
            json.dump(MAFIA_CFG, _f, ensure_ascii=False)
    except Exception as _e:
        log.warning(f"mafia save cfg: {_e}")


def mafia_get_group():
    return MAFIA_CFG.get("group") or MAFIA_GROUP


def mafia_get_admin_name():
    return MAFIA_CFG.get("admin_name") or ADMIN_NAME_IN_GAME
MAFIA_STATE = {}  # sid -> dict(role, alive, target_cmd, game_active)
MAFIA_RULES = {
    "Мафия":     {"never_kill_admin": True},
    "Дон":       {"never_kill_admin": True},
    "Маньяк":    {"never_kill_admin": True},
    "Комиссар":  {"never_check_admin": True},
    "Сержант":   {"never_check_admin": True},
    "Доктор":    {"always_heal_admin": True},
    "Любовница": {"never_distract_admin": True},
    "Адвокат":   {"always_defend_admin": True},
    "Камикадзе": {"never_take_admin": True},
}
MAFIA_ROLE_WORDS = [
    "Мафия","Дон","Комиссар","Сержант","Доктор","Маньяк",
    "Любовница","Адвокат","Самоубийца","Бомж","Счастливчик",
    "Камикадзе","Мирный житель","Мирный","Журналист",
]

MARVEL_SESSION = "5862417157"        # dreamer (@webgh6e)
DEFAULT_VOICE_CHAT = "https://t.me/chatikxxzs?videochat"
MARVEL_TIMEOUT = 20

ACTIVE_VOICE_SESSION = {"sid": None}  # None -> MARVEL_SESSION по умолчанию

# loop-режим
LOOP_STATE = {"on": True}

# монитор голосовых чатов (до 5, по приоритету)
MONITOR = {
    "user_stopped": False,  # пользователь вручную вышел — keeper не заходит
    "chats": [None, None, None, None, None],   # ссылки
    "media": None,       # путь к картинке/видео
    "on": False,
    "current": None,     # в каком чате сейчас играем
    "task": None,        # asyncio task
}   # включён по умолчанию

VOICE_TTS = {
    "chat_link": None,
    "marvel_voice_name": "гандон",
    "active": False,
}

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
MODELS = []
SETTINGS = {"ai_enabled": False, "trigger_every": TRIGGER_EVERY}

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
    # исключаем Сайкуна и Vortex
    EXCLUDE = {"8414522026", "8284866397"}
    sess = {k: v for k, v in sess.items() if k not in EXCLUDE}
    log.info(f"env сессий после фильтра: {len(sess)}")
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

    # мафия: навесить handlers
    from telethon import events as _ev_maf
    _mg = None
    if MAFIA_GROUP:
        try:
            for _c in CLIENTS.values():
                _mg = await _c.get_entity(MAFIA_GROUP)
                break
        except Exception as _e:
            log.warning(f"mafia group resolve: {_e}")
    _count = 0
    for _sid, _c in CLIENTS.items():
        try:
            _c.add_event_handler(mafia_dm_handler, _ev_maf.NewMessage(from_users=MAFIA_BOT))
            if _mg:
                _c.add_event_handler(mafia_group_handler, _ev_maf.NewMessage(chats=_mg))
            _count += 1
        except Exception as _e:
            log.warning(f"mafia handler {_sid}: {_e}")
    log.info(f"мафия: handlers навешены на {_count} сессий")


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




def main_menu():
    kb = [
        [KeyboardButton(text="📡 Монитор ГЧ"), KeyboardButton(text="📊 Статус")],
        [KeyboardButton(text="🔊 MP3 в войс"), KeyboardButton(text="⏱ МП3 на N сек")],
        [KeyboardButton(text="🎬 Медиа в войс"), KeyboardButton(text="🎬 Медиа на N сек")],
        [KeyboardButton(text="🎙 Войти в войс"), KeyboardButton(text="🚪 Выйти из войса")],
        [KeyboardButton(text="🎙 Войти на N сек"), KeyboardButton(text="🔄 Перезагрузить")],
        [KeyboardButton(text="🚪 Войти в чат"), KeyboardButton(text="📢 Войти в канал")],
        [KeyboardButton(text="🎭 Мафия"), KeyboardButton(text="📜 Логи")],
        [KeyboardButton(text="📋 Сессии"), KeyboardButton(text="📜 Логи")],
        [KeyboardButton(text="💬 Написать"), KeyboardButton(text="✉️ ЛС юзеру")],
        [KeyboardButton(text="🖼 Аватар"), KeyboardButton(text="✏️ Имя")],
        [KeyboardButton(text="🎭 Реакция"), KeyboardButton(text="👥 Участники")],
        [KeyboardButton(text="🚪 Выйти из чата"), KeyboardButton(text="🔄 Перезагрузить")],
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

    @dp.message(F.text == "🎭 Мафия")
    async def mafia_menu(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        g = mafia_get_group() or "не задана"
        n = mafia_get_admin_name() or "не задано"
        await m.answer(
            f"🎭 Мафия TrueMafiaBot\n\n"
            f"Группа: {g}\n"
            f"Твоё имя: {n}\n\n"
            "КОМАНДЫ:\n"
            "/mafia_group <ссылка> — задать группу\n"
            "  @username / t.me/+invite / -100xxx / https://t.me/+xxx\n"
            "/mafia_name <имя> — твоё имя в игре\n"
            "/mafia_show — показать настройки\n"
            "/mafia_join — завести всех и начать игру\n"
            "/mafia_status — кто с какой ролью\n"
            "/mafia_roles — сброс ролей\n\n"
            "В ГРУППЕ ПИШИ:\n"
            "убей Вася / голосуй Петя / проверь Маша / лечи Коля"
        )

    @dp.message(Command("mafia_join"))
    async def _mjoin(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        parts = m.text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else None
        # если аргумент похож на ссылку/юзернейм — используем как группу
        if arg and (arg.startswith("@") or "t.me/" in arg or arg.startswith("-100") or arg.startswith("+") or arg.startswith("https")):
            MAFIA_CFG["group"] = arg
            mafia_save_cfg()
            await m.answer(f"группа задана: {arg}\nзапускаю заход...")
            await mafia_join_group(None, arg)
            await m.answer("готово")
        else:
            sid = arg
            await m.answer(f"запускаю мафию (sid={sid or 'все'})...")
            await mafia_join_group(sid)
            await m.answer("готово")

    @dp.message(Command("mafia_group"))
    async def _mgroup(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        parts = m.text.split(maxsplit=1)
        if len(parts) < 2:
            cur = mafia_get_group() or "не задана"
            await m.answer(f"текущая группа: {cur}\n\nПрименение:\n/mafia_group @username\n/mafia_group https://t.me/+invitehash\n/mafia_group -1001234567890")
            return
        link = parts[1].strip()
        MAFIA_CFG["group"] = link
        mafia_save_cfg()
        await m.answer(f"✓ группа сохранена: {link}")

    @dp.message(Command("mafia_name"))
    async def _mname(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        parts = m.text.split(maxsplit=1)
        if len(parts) < 2:
            cur = mafia_get_admin_name() or "не задано"
            await m.answer(f"твоё игровое имя: {cur}\n\nПрименение:\n/mafia_name ыхыхх.k.vu")
            return
        name = parts[1].strip()
        MAFIA_CFG["admin_name"] = name
        mafia_save_cfg()
        await m.answer(f"✓ имя сохранено: {name}\nТеперь боты не будут тебя убивать/голосовать против.")

    @dp.message(Command("mafia_show"))
    async def _mshow(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        g = mafia_get_group() or "не задана"
        n = mafia_get_admin_name() or "не задано"
        cnt = len(CLIENTS)
        await m.answer(
            f"Настройки мафии:\n"
            f"Группа: {g}\n"
            f"Имя админа: {n}\n"
            f"Сессий онлайн: {cnt}\n\n"
            f"Команды:\n"
            f"/mafia_group <ссылка> — задать группу\n"
            f"/mafia_name <имя> — твоё имя в игре\n"
            f"/mafia_join — завести всех и начать\n"
            f"/mafia_status — роли\n"
            f"/mafia_roles — сброс"
        )

    @dp.message(Command("mafia_status"))
    async def _mst(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        lines = []
        for sid in CLIENTS:
            st = MAFIA_STATE.get(sid, {})
            r = st.get("role") or "?"
            a = "жив" if st.get("alive", True) else "мёртв"
            nm = CLIENT_META.get(sid, {}).get("name", sid)
            lines.append(f"{nm} ({sid}): {r} [{a}]")
        await m.answer("\n".join(lines) or "никто не онлайн")

    @dp.message(Command("mafia_roles"))
    async def _mr(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        for sid in MAFIA_STATE:
            MAFIA_STATE[sid].update(role=None, alive=True, target_cmd=None, game_active=False)
        await m.answer("роли сброшены")

    @dp.message(F.text == "📡 Монитор ГЧ")
    async def monitor_menu(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        chats = MONITOR["chats"]
        lines = []
        for i, link in enumerate(chats):
            mark = "▶" if MONITOR["current"] == link else " "
            lines.append(f"{mark} {i+1}. {link or '—'}")
        st = "🟢 вкл" if MONITOR["on"] else "⚪ выкл"
        media = os.path.basename(MONITOR["media"]) if MONITOR["media"] else "нет"
        text = (f"📡 МОНИТОР ГЧ\n\n"
                f"Статус: {st}\n"
                f"Медиа: {media}\n"
                f"Сейчас играю: {MONITOR['current'] or 'нигде'}\n\n"
                + "\n".join(lines))
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🟢 ВКЛ", callback_data="mon_on"),
             InlineKeyboardButton(text="⚪ ВЫКЛ", callback_data="mon_off")],
            [InlineKeyboardButton(text="🎬 Прислать медиа", callback_data="mon_media"),
             InlineKeyboardButton(text="🔄 Проверить", callback_data="mon_check")],
            [InlineKeyboardButton(text="1️⃣", callback_data="mon_add_0"),
             InlineKeyboardButton(text="2️⃣", callback_data="mon_add_1"),
             InlineKeyboardButton(text="3️⃣", callback_data="mon_add_2"),
             InlineKeyboardButton(text="4️⃣", callback_data="mon_add_3"),
             InlineKeyboardButton(text="5️⃣", callback_data="mon_add_4")],
            [InlineKeyboardButton(text="🗑 Очистить всё", callback_data="mon_clearall"),
             InlineKeyboardButton(text="🔁 Обновить", callback_data="mon_refresh")],
        ])
        await m.answer(text, reply_markup=kb)

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



    @dp.message(F.text == "🔄 Перезагрузить")
    async def rel(m: at.Message):
        if m.from_user.id != ADMIN_ID: return
        for c in CLIENTS.values():
            try: await c.disconnect()
            except: pass
        CLIENTS.clear(); CLIENT_META.clear()
        await load_env_sessions()
        _vc = CLIENTS.get(MARVEL_SESSION)
        if _vc:
            try:
                from telethon import events as _ev
                _vc.add_event_handler(voice_tts_dm_handler,
                                      _ev.NewMessage(func=lambda e: e.is_private))
            except Exception:
                pass
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
        if st.get("a") == "mon_set_media":
            file_obj = m.document or m.audio or (m.photo[-1] if m.photo else None) or m.video or m.animation
            if not file_obj:
                await m.answer("не понял что пришло"); return
            fname = "mon_media.bin"
            if hasattr(file_obj, "file_name") and file_obj.file_name:
                fname = file_obj.file_name
            elif m.photo:
                fname = "mon_media.jpg"
            elif m.video:
                fname = "mon_media.mp4"
            safe = re.sub(r"[^A-Za-z0-9._\-]", "_", fname)
            path = os.path.join(MEDIA_DIR, "monitor_" + str(int(time.time())) + "_" + safe)
            try:
                await bot.download(file_obj, destination=path)
            except Exception as e:
                await m.answer(f"не смог скачать: {e}"); return
            MONITOR["media"] = path
            sz = os.path.getsize(path)
            ADMIN_STATE.pop(m.from_user.id, None)
            await m.answer(f"✓ медиа для монитора ({sz // 1024}K). /mon on вкл, /mon off выкл")
            return

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

        # ========== МОНИТОР ==========
        d = cbq.data
        if d == "mon_on":
            MONITOR["on"] = True
            await cbq.answer("вкл")
            await cbq.message.answer("🟢 монитор вкл, проверка каждые 60с")
            return
        if d == "mon_off":
            MONITOR["on"] = False
            if MONITOR["current"]:
                try:
                    c = CLIENTS.get(MARVEL_SESSION)
                    e = await resolve_entity(c, MONITOR["current"]) if c else None
                    if e:
                        py = VOICE_CALLS.get(MARVEL_SESSION)
                        if py:
                            await py.leave_call(utils.get_peer_id(e))
                except Exception:
                    pass
                MONITOR["current"] = None
            await cbq.answer("выкл")
            await cbq.message.answer("⚪ монитор выкл")
            return
        if d == "mon_media":
            ADMIN_STATE[cbq.from_user.id] = {"a": "mon_set_media"}
            await cbq.answer()
            await cbq.message.answer("пришли фото или видео для показа")
            return
        if d == "mon_check":
            c = CLIENTS.get(MARVEL_SESSION)
            if not c:
                await cbq.answer("нет сессии", show_alert=True)
                return
            await cbq.answer("проверяю...")
            lines = []
            for i, link in enumerate(MONITOR["chats"]):
                if not link:
                    lines.append(f"{i+1}. —")
                    continue
                e = await resolve_entity(c, link)
                if not e:
                    lines.append(f"{i+1}. не найден")
                    continue
                active = await check_voice_active(c, e)
                lines.append(f"{i+1}. {'🟢 активен' if active else '⚪ нет войса'}")
            await cbq.message.answer("проверка:\n" + "\n".join(lines))
            return
        if d == "mon_clearall":
            MONITOR["chats"] = [None, None, None, None, None]
            await cbq.answer("очищено")
            await cbq.message.answer("🗑 все чаты очищены")
            return
        if d == "mon_refresh":
            await cbq.answer("жми 📡 Монитор ГЧ снова")
            return
        if d.startswith("mon_add_"):
            idx = int(d.split("_")[2])
            ADMIN_STATE[cbq.from_user.id] = {"a": "mon_add_link", "idx": idx}
            await cbq.answer()
            await cbq.message.answer(f"пришли ссылку для чата №{idx+1}:")
            return
        # ============================


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

        # ==== монитор ====
        if a == "mon_add_link":
            idx = st.get("idx", 0)
            link = m.text.strip()
            MONITOR["chats"][idx] = link
            ADMIN_STATE.pop(m.from_user.id, None)
            await m.answer(f"✓ чат №{idx+1} = {link}")
            return

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
        if LOOP_STATE.get("on", True) and dur > 2:
            period = max(1.0, dur - 1.5)

            async def auto_loop(sid, e):
                while True:
                    await asyncio.sleep(period)
                    if sid not in VOICE_CALLS:
                        return
                    if not LOOP_STATE.get("on", True):
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
    if not mp3 or not os.path.exists(mp3):
        await bot.send_message(uid, "файл не найден"); return

    dur = mp3_duration(mp3)
    if dur <= 0:
        dur = 60

    entered = []
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
            entered.append((sid, e, time.time()))
            await bot.send_message(uid, f"▶ {CLIENT_META[sid]['name']} ({dur:.0f}с)")
        except Exception as ex:
            await bot.send_message(uid, f"✗ {CLIENT_META[sid]['name']}: {ex}")
        await asyncio.sleep(1.0)

    if not entered:
        return

    if LOOP_STATE.get("on", True) and dur > 2:
        await bot.send_message(uid, f"🔁 mp3 по кругу. /stop чтобы выйти")
        period = max(1.0, dur - 1.5)

        async def auto_loop(sid, e):
            while True:
                await asyncio.sleep(period)
                if sid not in VOICE_CALLS:
                    return
                if not LOOP_STATE.get("on", True):
                    return
                try:
                    py = VOICE_CALLS[sid]
                    await py.play(utils.get_peer_id(e), MediaStream(mp3))
                except Exception:
                    return

        for sid, e, _ in entered:
            asyncio.create_task(auto_loop(sid, e))
    else:
        await bot.send_message(uid, f"▶ играет один раз")


# ==================== MAIN ====================
def find_session_by_name(query: str):
    """Ищет сессию по приблизительному нику. Возвращает sid или None."""
    q = query.lower().strip().lstrip("@")
    if not q:
        return None
    # точное совпадение имени
    for sid, meta in CLIENT_META.items():
        nm = (meta.get("name") or "").lower()
        if nm == q:
            return sid
    # частичное — начало имени
    for sid, meta in CLIENT_META.items():
        nm = (meta.get("name") or "").lower()
        if nm.startswith(q):
            return sid
    # частичное — вхождение
    for sid, meta in CLIENT_META.items():
        nm = (meta.get("name") or "").lower()
        if q in nm:
            return sid
    # по id
    if q.isdigit() and q in CLIENT_META:
        return q
    return None


def get_voice_sid():
    """Возвращает активную сессию для озвучки (по умолчанию Сайкун)."""
    sid = ACTIVE_VOICE_SESSION.get("sid")
    if sid and sid in CLIENTS:
        return sid
    return MARVEL_SESSION


async def marvel_get_voice(text: str):
    """Отправляет Marvel, ждёт голосовое через event handler (моментально)."""
    # Marvel всегда через Сайкуна
    c = CLIENTS.get(MARVEL_SESSION)
    if not c:
        log.warning(f"marvel: нет сессии {MARVEL_SESSION}")
        return None
    try:
        bot_ent = await c.get_entity(MARVEL_BOT_USERNAME)
    except Exception as e:
        log.warning(f"marvel: не нашёл бота: {e}")
        return None

    got = asyncio.Event()
    result = {"path": None}

    async def on_new(event):
        try:
            sender = await event.get_sender()
            if not sender or sender.id != bot_ent.id:
                return
            m = event.message
            is_audio = bool(m.voice or m.audio or (
                m.document and m.document.mime_type and "audio" in m.document.mime_type
            ))
            if not is_audio:
                return
            # скачиваем сразу
            path = await m.download_media(file=TTS_DIR)
            if path and os.path.exists(path) and os.path.getsize(path) > 500:
                result["path"] = path
                got.set()
        except Exception as ex:
            log.warning(f"marvel handler: {ex}")

    from telethon import events as _ev
    c.add_event_handler(on_new, _ev.NewMessage(chats=bot_ent.id))
    try:
        await c.send_message(bot_ent, "/start")
        await asyncio.sleep(0.25)
        await c.send_message(bot_ent, "🎙 Текст в голос")
        await asyncio.sleep(0.25)
        await c.send_message(bot_ent, VOICE_TTS.get("marvel_voice_name", "гандон"))
        await asyncio.sleep(0.25)
        await c.send_message(bot_ent, text)
        log.info(f"marvel: отправил '{text[:40]}'")
        try:
            await asyncio.wait_for(got.wait(), timeout=20)
        except asyncio.TimeoutError:
            log.warning("marvel: таймаут 20с")
    finally:
        try:
            c.remove_event_handler(on_new)
        except Exception:
            pass
    return result["path"]




async def voice_tts_dm_handler(event):
    """ЛС vortex от ADMIN_ID. Текст -> голосовое Сайкуна в его войсе."""
    try:
        sender = await event.get_sender()
        if not sender or sender.id != ADMIN_ID:
            return
        text = (event.message.text or "").strip()
        if not text:
            return

        low = text.lower()

        if low.startswith("/voice "):
            new_name = text.split(maxsplit=1)[1].strip()
            VOICE_TTS["marvel_voice_name"] = new_name
            await event.reply(f"голос: {new_name}")
            return

        if low in ("/stop", "stop", "стоп"):
            sid = MARVEL_SESSION
            py = VOICE_CALLS.get(sid)
            if py and VOICE_TTS.get("chat_link"):
                c = CLIENTS.get(sid)
                if c:
                    e = await resolve_entity(c, VOICE_TTS["chat_link"])
                    if e:
                        try:
                            await py.leave_call(utils.get_peer_id(e))
                        except Exception:
                            pass
            VOICE_CALLS.pop(sid, None)
            VOICE_TTS["chat_link"] = None
            VOICE_TTS["active"] = False
            await event.reply("вышел")
            return

        if low.startswith("/set "):
            link = text.split(maxsplit=1)[1].strip()
            vid = get_voice_sid()
            mc = CLIENTS.get(vid)
            if not mc:
                await event.reply(f"сессия {vid} не подключена")
                return
            e = await resolve_entity(mc, link)
            if not e:
                await event.reply("не смог найти чат")
                return
            try:
                await mc(JoinChannelRequest(e))
            except Exception:
                pass
            try:
                py = VOICE_CALLS.get(vid)
                if not py:
                    py = PyTgCalls(mc)
                    await py.start()
                    VOICE_CALLS[vid] = py
                if os.path.exists(SILENT_OGG):
                    await py.play(utils.get_peer_id(e), MediaStream(SILENT_OGG))
                VOICE_TTS["chat_link"] = link
                VOICE_TTS["active"] = True
                log.info(f"VOICE_TTS установлен: {vid} -> {link}")
                meta = CLIENT_META.get(vid, {})
                nm = meta.get("name", vid)
                await event.reply(f"✓ {nm} в войсе: {link}")
            except Exception as ex:
                await event.reply(f"войти не вышло: {ex}")
            return

        if low.startswith("/m "):
            query = text.split(maxsplit=1)[1].strip()
            sid = find_session_by_name(query)
            if not sid:
                await event.reply(f"не нашёл сессию по '{query}'")
                return
            ACTIVE_VOICE_SESSION["sid"] = sid
            meta = CLIENT_META.get(sid, {})
            nm = meta.get("name", sid)
            await event.reply(f"✓ активная сессия: {nm} ({sid})\nТеперь озвучиваю через него. /set <ссылка> чтобы выбрать его войс.")
            return

        if low == "/who" or low == "/whoami":
            vid = get_voice_sid()
            meta = CLIENT_META.get(vid, {})
            nm = meta.get("name", vid)
            in_voice = "в войсе" if vid in VOICE_CALLS else "не в войсе"
            link = VOICE_TTS.get("chat_link") or "не установлен"
            await event.reply(f"активная: {nm} ({vid})\nстатус: {in_voice}\nвойс: {link}")
            return

        if low in ("/loop", "/loop on"):
            LOOP_STATE["on"] = True
            await event.reply("🔁 loop включён")
            return
        if low == "/loop off":
            LOOP_STATE["on"] = False
            await event.reply("➡️ loop выключен")
            return

        # ========== монитор ==========
        if low == "/mon on":
            MONITOR["on"] = True
            MONITOR["user_stopped"] = False
            await event.reply("✅ монитор вкл (проверка каждые 60с)")
            return
        if low == "/mon off":
            MONITOR["on"] = False
            # выйти из чата если играем
            if MONITOR["current"]:
                try:
                    e = await resolve_entity(CLIENTS[MARVEL_SESSION], MONITOR["current"])
                    if e:
                        py = VOICE_CALLS.get(MARVEL_SESSION)
                        if py:
                            await py.leave_call(utils.get_peer_id(e))
                except Exception:
                    pass
                MONITOR["current"] = None
            await event.reply("❌ монитор выкл")
            return
        if low.startswith("/mon add "):
            parts = text.split(maxsplit=2)
            if len(parts) < 3:
                await event.reply("использование: /mon add <1-5> <ссылка>")
                return
            try:
                idx = int(parts[1]) - 1
                if not 0 <= idx <= 4:
                    raise ValueError
            except Exception:
                await event.reply("номер 1-5")
                return
            MONITOR["chats"][idx] = parts[2].strip()
            await event.reply(f"✓ чат {idx+1} = {parts[2].strip()}")
            return
        if low.startswith("/mon clear"):
            parts = text.split()
            if len(parts) == 1:
                MONITOR["chats"] = [None, None, None, None, None]
                await event.reply("очищены все")
                return
            try:
                idx = int(parts[1]) - 1
                if 0 <= idx <= 4:
                    MONITOR["chats"][idx] = None
                    await event.reply(f"✓ чат {idx+1} очищен")
            except Exception:
                pass
            return
        if low == "/mon list":
            lines = []
            for i, link in enumerate(MONITOR["chats"]):
                lines.append(f"{i+1}. {link or '-'}")
            st = "вкл" if MONITOR["on"] else "выкл"
            media = os.path.basename(MONITOR["media"]) if MONITOR["media"] else "нет"
            await event.reply(f"монитор: {st}\nмедиа: {media}\n" + "\n".join(lines))
            return
        if low == "/mon media":
            await event.reply("пришли медиа (фото/видео) следующим сообщением")
            ADMIN_STATE[event.sender_id] = {"a": "mon_set_media"}
            return
        if low == "/mon check":
            await event.reply("проверяю все чаты...")
            c = CLIENTS.get(MARVEL_SESSION)
            if not c:
                await event.reply("нет сессии")
                return
            lines = []
            for i, link in enumerate(MONITOR["chats"]):
                if not link:
                    lines.append(f"{i+1}. -")
                    continue
                e = await resolve_entity(c, link)
                if not e:
                    lines.append(f"{i+1}. {link} — не найден")
                    continue
                active = await check_voice_active(c, e)
                lines.append(f"{i+1}. {link} — {'🟢 активен' if active else '⚪ нет войса'}")
            await event.reply("\n".join(lines))
            return

        if low in ("/where", "где"):
            if VOICE_TTS.get("chat_link"):
                await event.reply(f"в войсе: {VOICE_TTS['chat_link']}")
            else:
                await event.reply("не в войсе")
            return

        if low == "/help":
            await event.reply(
                "пиши текст — озвучу голосом dreamer (@webgh6e).\n\n"
                "TTS:\n"
                "/m <ник> — аккаунт для озвучки\n"
                "/set <ссылка> — зайти в войс\n"
                "/voice <имя> — голос Marvel\n"
                "/stop — выйти\n\n"
                "МОНИТОР ГЧ (до 5 чатов по приоритету):\n"
                "/mon on|off — вкл/выкл\n"
                "/mon add <1-5> <ссылка> — задать чат\n"
                "/mon list — показать\n"
                "/mon clear — очистить\n"
                "/mon media — прислать картинку/видео для показа\n"
                "/mon check — проверить где сейчас войс\n\n"
                "loop:\n"
                "/loop on|off"
            )
            return

        vid = get_voice_sid()
        if vid not in VOICE_CALLS:
            meta = CLIENT_META.get(vid, {})
            nm = meta.get("name", vid)
            await event.reply(f"{nm} не в войсе. Зайди через 🎙 Войти в войс → ссылка → {nm}")
            return
        if not VOICE_TTS.get("chat_link"):
            await event.reply("Не знаю в какой войс зашёл Сайкун. Зайди заново через 🎙 Войти в войс.")
            return

        mp3 = await marvel_get_voice(text)
        if not mp3:
            await event.reply("Marvel не ответил")
            return
        vid = get_voice_sid()
        mc = CLIENTS.get(vid)
        if not mc:
            await event.reply(f"сессия {vid} не подключена")
            return
        e = await resolve_entity(mc, VOICE_TTS["chat_link"])
        if not e:
            await event.reply("чат пропал")
            return
        try:
            py = VOICE_CALLS.get(vid)
            if not py:
                py = PyTgCalls(mc)
                await py.start()
                VOICE_CALLS[vid] = py
            await py.play(utils.get_peer_id(e), MediaStream(mp3))
            await event.reply("▶")
        except Exception as ex:
            await event.reply(f"ошибка play: {ex}")
    except Exception as e:
        log.warning(f"voice_tts_dm: {e}")


async def auto_join_voice():
    """Заводит dreamer в DEFAULT_VOICE_CHAT и играет silent.ogg."""
    c = CLIENTS.get(MARVEL_SESSION)
    if not c:
        log.warning(f"auto_join: нет сессии {MARVEL_SESSION}")
        return False
    e = await resolve_entity(c, DEFAULT_VOICE_CHAT)
    if not e:
        log.warning(f"auto_join: не могу резолвить {DEFAULT_VOICE_CHAT}")
        return False
    try:
        await c(JoinChannelRequest(e))
    except Exception:
        pass
    try:
        py = VOICE_CALLS.get(MARVEL_SESSION)
        if not py:
            py = PyTgCalls(c)
            await py.start()
            VOICE_CALLS[MARVEL_SESSION] = py
        if os.path.exists(SILENT_OGG):
            await py.play(utils.get_peer_id(e), MediaStream(SILENT_OGG))
        VOICE_TTS["chat_link"] = DEFAULT_VOICE_CHAT
        VOICE_TTS["active"] = True
        ACTIVE_VOICE_SESSION["sid"] = MARVEL_SESSION
        log.info(f"auto_join: {MARVEL_SESSION} в войсе {DEFAULT_VOICE_CHAT}")
        return True
    except Exception as ex:
        log.warning(f"auto_join play: {ex}")
        return False


async def voice_keeper():
    """Каждые 30с проверяет что dreamer в войсе. Если нет — заходит снова.
       Каждые 40 минут перезапускает silent.ogg (он заканчивается через час)."""
    last_refresh = 0.0
    first_run = True
    while True:
        try:
            await asyncio.sleep(30)
            # если в первые 10 сек после старта ещё не зашли — пробуем каждые 30с
            vid = MARVEL_SESSION
            c = CLIENTS.get(vid)
            if not c:
                continue

            # если его нет в VOICE_CALLS или протухло — перезайти
            if vid not in VOICE_CALLS:
                if MONITOR.get("user_stopped"):
                    continue  # пользователь сам вышел, не лезем
                ok = await auto_join_voice()
                if ok:
                    last_refresh = time.time()
                continue

            # перезапуск silent раз в 40 минут чтобы не отваливался
            now = time.time()
            if now - last_refresh > 2400:
                e = await resolve_entity(c, DEFAULT_VOICE_CHAT)
                if e and os.path.exists(SILENT_OGG):
                    try:
                        py = VOICE_CALLS.get(vid)
                        await py.play(utils.get_peer_id(e), MediaStream(SILENT_OGG))
                        last_refresh = now
                        log.info("voice_keeper: silent.ogg перезапущен")
                    except Exception as ex:
                        log.warning(f"voice_keeper refresh: {ex}")
        except Exception as e:
            log.warning(f"voice_keeper: {e}")


async def check_voice_active(client, ent):
    """Есть ли активный голосовой чат в группе/канале."""
    try:
        full = await client(GetFullChannelRequest(ent))
        call = getattr(full.full_chat, "call", None)
        return call is not None
    except Exception as e:
        log.warning(f"check_voice_active: {e}")
        return False


async def monitor_once():
    """Одна проверка: найти первый активный чат по приоритету, при необходимости перейти и играть медиа."""
    if not MONITOR["on"]:
        return
    if not MONITOR["media"]:
        return
    if not any(MONITOR["chats"]):
        return

    c = CLIENTS.get(MARVEL_SESSION)
    if not c:
        return

    # найти первый активный чат
    target = None
    for link in MONITOR["chats"]:
        if not link:
            continue
        e = await resolve_entity(c, link)
        if not e:
            continue
        if await check_voice_active(c, e):
            target = link
            break

    if not target:
        log.info("monitor: ни один ГЧ не активен")
        # если мы были в чате и он перестал быть активным — выйти
        if MONITOR["current"]:
            old_e = await resolve_entity(c, MONITOR["current"])
            if old_e:
                try:
                    py = VOICE_CALLS.get(MARVEL_SESSION)
                    if py:
                        await py.leave_call(utils.get_peer_id(old_e))
                except Exception:
                    pass
            MONITOR["current"] = None
        return

    if MONITOR["current"] == target:
        return  # уже там

    # переключение
    log.info(f"monitor: переключаюсь на {target}")
    if MONITOR["current"]:
        old_e = await resolve_entity(c, MONITOR["current"])
        if old_e:
            try:
                py = VOICE_CALLS.get(MARVEL_SESSION)
                if py:
                    await py.leave_call(utils.get_peer_id(old_e))
            except Exception:
                pass

    e = await resolve_entity(c, target)
    if not e:
        return
    try:
        await c(JoinChannelRequest(e))
    except Exception:
        pass
    try:
        py = VOICE_CALLS.get(MARVEL_SESSION)
        if not py:
            py = PyTgCalls(c)
            await py.start()
            VOICE_CALLS[MARVEL_SESSION] = py

        media = MONITOR["media"]
        # конвертим если картинка/видео
        kind = media_kind(media)
        if kind in ("image", "video", "gif"):
            conv = await convert_media(media)
            if conv:
                media = conv
        await py.play(utils.get_peer_id(e), MediaStream(media))
        MONITOR["current"] = target
        log.info(f"monitor: играю {media}")
    except Exception as ex:
        log.warning(f"monitor play: {ex}")


async def monitor_loop():
    """Каждые 60 сек проверяет ГЧ."""
    while True:
        try:
            await asyncio.sleep(60)
            await monitor_once()
        except Exception as e:
            log.warning(f"monitor_loop: {e}")


# ================== МАФИЯ ==================
def mafia_state(sid):
    if sid not in MAFIA_STATE:
        MAFIA_STATE[sid] = {
            "role": None, "alive": True, "target_cmd": None, "game_active": False,
        }
    return MAFIA_STATE[sid]


def mafia_parse_role(text):
    m = re.search(r"Ты\s*-\s*[^\w]*\s*([А-Яа-яЁёA-Za-z]+(?:\s+[А-Яа-яЁёA-Za-z]+)?)", text)
    if m:
        role = m.group(1).strip()
        for r in MAFIA_ROLE_WORDS:
            if r.lower() in role.lower():
                return r
        return role
    low = text.lower()
    for r in MAFIA_ROLE_WORDS:
        if r.lower() in low:
            return r
    return None


def mafia_is_night(text):
    low = text.lower()
    return any(k in low for k in [
        "кого ты хочешь линчевать", "с кем будем спать", "кого будем лечить",
        "выбери игрока", "кого убить", "кого проверить", "к кому пойти",
        "твой ход", "выбери жертву",
    ])


def mafia_is_vote(text):
    low = text.lower()
    return any(k in low for k in [
        "пришло время определить", "наказать виноватых",
        "голосование продлится", "голосуй", "кого казнить",
    ])


def mafia_is_confirm(text):
    low = text.lower()
    return any(k in low for k in [
        "вы точно хотите линчевать", "подтверди", "подтвердите", "точно ли",
    ])


def mafia_pick_button(buttons, sid, kind):
    st = mafia_state(sid)
    role = st["role"] or ""
    rules = MAFIA_RULES.get(role, {})
    admin_id = str(ADMIN_ID)
    admin_name_low = ADMIN_NAME_IN_GAME.lower() if ADMIN_NAME_IN_GAME else None
    flat = [b for row in buttons for b in row]

    cmd = st.get("target_cmd")
    if cmd and kind in ("night", "vote"):
        c = cmd.lower()
        for b in flat:
            if c in (b.text or "").lower():
                return b

    if kind == "night":
        if rules.get("never_kill_admin") or rules.get("never_check_admin") \
           or rules.get("never_distract_admin"):
            for b in flat:
                txt = (b.text or "")
                if admin_id not in txt and (not admin_name_low or admin_name_low not in txt.lower()):
                    return b
        if rules.get("always_heal_admin") or rules.get("always_defend_admin"):
            for b in flat:
                txt = (b.text or "")
                if admin_id in txt or (admin_name_low and admin_name_low in txt.lower()):
                    return b
            return flat[0] if flat else None

    if kind == "vote":
        if rules.get("never_kill_admin"):
            for b in flat:
                txt = (b.text or "")
                if admin_id not in txt and (not admin_name_low or admin_name_low not in txt.lower()):
                    return b

    if kind == "confirm":
        for b in flat:
            t = (b.text or "").lower()
            if any(x in t for x in ["да", "подтверд", "yes", "ok", "✅", "за"]):
                return b
        return flat[0] if flat else None

    return flat[0] if flat else None


async def mafia_click(client, msg, btn):
    try:
        if hasattr(btn, "data") and btn.data:
            await client(GetBotCallbackAnswerRequest(
                peer=msg.peer_id, msg_id=msg.id, data=btn.data))
            return True
        await msg.click(text=btn.text)
        return True
    except Exception as e:
        log.warning(f"mafia click: {e}")
        return False


async def mafia_dm_handler(event):
    """Обработка ЛС от @TrueMafiaBot."""
    try:
        me = await event.client.get_me()
        sid = None
        for s, meta in CLIENT_META.items():
            if meta.get("id") == me.id:
                sid = s; break
        if not sid:
            return
        st = mafia_state(sid)
        msg = event.message
        text = msg.text or ""
        buttons = msg.buttons or []

        log.info(f"[МАФИЯ {sid}] ЛС: {text[:130]!r}")
        for row in buttons:
            log.info(f"  [{sid}] btn: {[b.text for b in row]}")

        r = mafia_parse_role(text)
        if r:
            st["role"] = r
            st["game_active"] = True
            st["alive"] = True
            log.info(f"[{sid}] РОЛЬ = {r}")

        low = text.lower()
        if any(k in low for k in ["тебя убили", "тебя линчевали", "ты мёртв", "вы мертвы", "погиб"]):
            st["alive"] = False
            log.info(f"[{sid}] МЁРТВ")

        if mafia_is_night(text) and buttons:
            btn = mafia_pick_button(buttons, sid, "night")
            if btn:
                ok = await mafia_click(event.client, msg, btn)
                log.info(f"[{sid}] night -> {btn.text} ok={ok}")
                st["target_cmd"] = None
            return

        if mafia_is_vote(text) and buttons:
            btn = mafia_pick_button(buttons, sid, "vote")
            if btn:
                ok = await mafia_click(event.client, msg, btn)
                log.info(f"[{sid}] vote -> {btn.text} ok={ok}")
                st["target_cmd"] = None
            return

        if mafia_is_confirm(text) and buttons:
            btn = mafia_pick_button(buttons, sid, "confirm")
            if btn:
                ok = await mafia_click(event.client, msg, btn)
                log.info(f"[{sid}] confirm -> {btn.text} ok={ok}")
            return
    except Exception as e:
        log.warning(f"mafia_dm: {e}")


async def mafia_group_handler(event):
    """Твои команды в группе — рассылаем всем ботам."""
    try:
        sender = await event.get_sender()
        if not sender or getattr(sender, "id", 0) != ADMIN_ID:
            return
        text = (event.message.text or "").strip()
        if not text:
            return
        low = text.lower()
        m = re.match(
            r"^(убей|kill|голосуй|vote|проверь|check|лечи|heal|отвлеки|защити|"
            r"кик|выгони|казни|линчуй|исключи|забань|кикайте|голосуем|убиваем|"
            r"проверяем|лечим|казним|выгоняем|линчуем)\s+(.+)$",
            low,
        )
        if m:
            target = m.group(2).strip()
            for sid in MAFIA_STATE:
                MAFIA_STATE[sid]["target_cmd"] = target
            log.info(f"МАФИЯ КОМАНДА: {m.group(1)} -> {target}")
    except Exception as e:
        log.warning(f"mafia_group: {e}")


async def mafia_resolve_group(client, link):
    """Резолвит группу: @username, t.me/+invite, t.me/joinchat/xxx, ID."""
    if not link:
        return None
    link = link.strip()
    # инвайт-ссылка
    m = re.search(r"(?:t\.me/\+|joinchat/)([A-Za-z0-9_\-]+)", link)
    if m:
        hash_part = m.group(1)
        try:
            from telethon.tl.functions.messages import ImportChatInviteRequest
            upd = await client(ImportChatInviteRequest(hash_part))
            chats = getattr(upd, "chats", [])
            if chats:
                return chats[0]
        except Exception as e:
            if "already" in str(e).lower():
                # уже там — просто резолвим через check
                try:
                    from telethon.tl.functions.messages import CheckChatInviteRequest
                    info = await client(CheckChatInviteRequest(hash_part))
                    if getattr(info, "chat", None):
                        return info.chat
                except Exception:
                    pass
        return None
    # @username или t.me/username
    uname = link
    mm = re.match(r"(?:https?://)?t\.me/([A-Za-z0-9_]+)", link)
    if mm:
        uname = mm.group(1)
    if uname.startswith("@"):
        uname = uname[1:]
    try:
        return await client.get_entity(uname)
    except Exception:
        return None


async def mafia_join_group(sid=None, group_link=None):
    """Заводит ботов в группу и нажимает присоединиться."""
    link = group_link or mafia_get_group()
    if not link:
        log.warning("группа не задана")
        return
    targets = [sid] if sid else list(CLIENTS.keys())
    first = True
    ok_count = 0
    for s in targets:
        c = CLIENTS.get(s)
        if not c:
            continue
        try:
            group = await mafia_resolve_group(c, link)
            if not group:
                log.warning(f"[МАФИЯ {s}] не смог резолвить {link}")
                await asyncio.sleep(1.5)
                continue
            try:
                await c(JoinChannelRequest(group))
            except Exception as e:
                if "already" not in str(e).lower():
                    log.warning(f"[МАФИЯ {s}] join: {e}")
            if first:
                await c.send_message(group, "/game")
                first = False
                await asyncio.sleep(3)
            clicked = False
            async for m in c.iter_messages(group, limit=10):
                if m.buttons:
                    for row in m.buttons:
                        for b in row:
                            t = (b.text or "").lower()
                            if any(x in t for x in ["присоед", "join", "участв"]):
                                await m.click(text=b.text)
                                clicked = True
                                break
                    if clicked:
                        break
            log.info(f"[МАФИЯ {s}] {'присоединился' if clicked else 'кнопка не найдена'}")
            ok_count += 1
        except Exception as e:
            log.warning(f"mafia_join {s}: {e}")
        await asyncio.sleep(random.uniform(1.5, 3.0))
    log.info(f"мафия: зашло {ok_count}/{len(targets)} сессий в {link}")


async def main():
    log.info(f"админ: {ADMIN_ID}, ИИ сессия: {AI_SESSION_ID}")
    await load_env_sessions()

    # привязка TTS handler к vortex
    _vc = CLIENTS.get(MARVEL_SESSION)
    if _vc:
        try:
            from telethon import events as _ev
            _vc.add_event_handler(voice_tts_dm_handler,
                                  _ev.NewMessage(func=lambda e: e.is_private))
            log.info(f"TTS handler навешен на {MARVEL_SESSION}")
        except Exception as _e:
            log.warning(f"add_event_handler: {_e}")

    if not CLIENTS:
        log.error("нет сессий")

    # автозаход dreamer в войс + контроль
    await asyncio.sleep(2)
    ok = await auto_join_voice()
    if ok:
        log.info("dreamer сидит в войсе")
    asyncio.create_task(voice_keeper())
    log.info("voice_keeper запущен")

    # монитор ГЧ (запускается всегда, но работает если MONITOR["on"])
    asyncio.create_task(monitor_loop())
    log.info("monitor_loop запущен")

    # AI отключён
    # # AI выключен

    if bot:
        log.info("управляющий бот стартует")
        await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
