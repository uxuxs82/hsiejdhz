import asyncio
import json
import logging
import os
import random
import re
import socket
import time
import urllib.request
import urllib.error
from collections import deque, defaultdict

from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, UserAlreadyParticipantError
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.functions.channels import JoinChannelRequest, GetFullChannelRequest

socket.setdefaulttimeout(20)

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
TG_STRING_SESSION = os.environ.get("TG_STRING_SESSION", "")

ANYMODEL_API_KEY = os.environ.get("ANYMODEL_API_KEY", "")
ANYMODEL_BASE_URL = os.environ.get("ANYMODEL_BASE_URL", "https://anymodel.org/v1")

MY_NAME = os.environ.get("MY_NAME", "vortex")

WATCH_CHATS = [
    -1002828764783,
    "@BhopProChat",
    "@asati_chat",
    "@lackachat",
    "@noomiclone117",
]

IGNORED_BOTS = ["valyutaTG_bot", "themetrbot", "iris_bs_bot", "iris_cm_bot", "ZanAIsuka_bot", "MarvelVoiceBot"]
NO_BUTTON_BOTS = ["iris_bs_bot", "iris_cm_bot", "valyutaTG_bot", "MarvelVoiceBot"]

TRIGGER_EVERY = 3
IDLE_TRIGGER_SEC = 60
CONTEXT_SIZE = 50
HISTORY_SIZE = 100
POLL_INTERVAL = 1
POLL_LIMIT = 15
AI_TIMEOUT = 30
MAX_AI_TOTAL = 90

ANYMODEL_MODELS = [
    "qwen/qwen3.7-max",
    "glm/glm-5.3-flash",
    "ds/deepseek-v4-flash",
    "qwen/qwen3.8-max",
    "qwen/qwen3.7-plus",
    "ag/gemini-2.5-flash",
    "ag/gemini-pro-agent",
    "kmc/kimi-for-coding",
    "glm/glm-5.2",
    "glm/glm-5.1",
    "glm/glm-5",
    "glm/glm-4.7",
    "cx/gpt-5.6-luna",
    "cx/gpt-6-luna",
]

MEMORY_BOT_MSGS = 200
MEMORY_USERS = 100
MEMORY_CONFLICTS = 50
MEMORY_DIALOG = 15
MEMORY_SEEN = 5000

MEM_DIR = os.environ.get("TG_MEM_DIR", "/data")
try:
    os.makedirs(MEM_DIR, exist_ok=True)
except Exception:
    MEM_DIR = "/tmp"
    os.makedirs(MEM_DIR, exist_ok=True)

MEMORY_FILE = f"{MEM_DIR}/memory.json"
REPLIED_FILE = f"{MEM_DIR}/replied.json"
SEEN_FILE = f"{MEM_DIR}/seen.json"

BAD_WORDS = ["pollinations","subscribe","sponsor","donate","подпишись","поддержк","реклам","промокод","casino","казино","ставк","букмекер"]
BAD_MARKERS = ["we need","the format","response to","message number","as an ai","i cannot","i can't","as a language model","user asks","we should","we can respond","let me","let's"]
STOP_WORDS = {"дроч","дрочить","дрочка","хм","эм","мм","прив","привет","ку","да","нет","ну","ага","угу","ок","окей","лол","ахах","ахаха","ахахах","хах","хаха","кек","кекв","жиза","ору","ржу","ржунимагу","жесть","пиздец","бля","блять","чо","чё","шо","?","...",".",",","-","+","=","*","/"}

SYSTEM_PROMPT = """ты школьник 15 лет, тебя зовут vortex. отвечай на сообщение в чате.

как читать чат:
- сообщения идут по порядку: #1, #2, #3...
- пометка (БОТ) значит что это игровой бот, не человек
- пометка (ОТВЕЧАЕТ имя) значит человек отвечает ЭТОМУ имени, а не тебе
- не путай кто кому пишет - смотри внимательно на пометки
- если человек пишет "тебе" или "ты" - это к тому кому он отвечает, не к тебе

ГЛАВНОЕ ПРАВИЛО:
- НИКОГДА не выдумывай факты, имена, события которых нет в сообщениях
- не придумывай людей которых нет в чате
- говори ТОЛЬКО по факту из сообщений
- если не уверен в факте - не утверждай, а спроси или пошути
- 'решение: нет' пиши ТОЛЬКО если реально нечего сказать

стиль:
- строчными буквами
- без точек и запятых
- максимум 12 слов
- как живой школьник с телефона, не наивный, не черезмерно добрый
- не признавайся что ты ии
- НЕ используй слово 'бро', 'друг', 'дружище'
- не подлизывайся, не соглашайся со всем
- можешь огрызнуться, подколоть, ответить с сарказмом
- если тема тупая — так и скажи коротко
- отвечай разнообразно, не повторяй одну и ту же фразу
- можно мат, но в меру, без оскорблений личности

формат:
решение: <номер сообщения>
сообщения:
<текст>

или:
решение: нет
"""

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("ai_chat")


def is_skip_msg(text):
    if not text: return True
    s = text.strip()
    if s.startswith(".") or s.startswith("/") or s.startswith("!"): return True
    if len(s) < 4: return True
    if not any(c.isalnum() for c in s): return True
    low = s.lower().rstrip("!?.,;:")
    if low in STOP_WORDS: return True
    ws = low.split()
    if len(ws) == 1 and ws[0] in STOP_WORDS: return True
    return False


def is_bad_response(t):
    if not t: return True
    low = t.lower()
    for w in BAD_WORDS:
        if w in low: return True
    for m in BAD_MARKERS:
        if m in low: return True
    if "http://" in low or "https://" in low or "www." in low: return True
    if "t.me/" in low or "telegram.me/" in low: return True
    if re.search(r"@[a-zA-Z0-9_]{3,}", t): return True
    letters = [c for c in t if c.isalpha()]
    if letters:
        up = sum(1 for c in letters if c.isupper())
        if up / len(letters) > 0.5: return True
    if len(re.findall(r"\b[a-zA-Z]{4,}\b", t)) > 3: return True
    return False


class Memory:
    def __init__(self):
        self.bot_messages = defaultdict(lambda: deque(maxlen=MEMORY_BOT_MSGS))
        self.users = defaultdict(lambda: deque(maxlen=MEMORY_USERS))
        self.conflicts = defaultdict(lambda: deque(maxlen=MEMORY_CONFLICTS))
        self.dialogs = defaultdict(lambda: defaultdict(lambda: deque(maxlen=MEMORY_DIALOG)))
        self.replied_ids = set()
        self.seen = defaultdict(set)
        self.seen_order = defaultdict(deque)
        self.load()

    def load(self):
        if os.path.exists(MEMORY_FILE):
            try:
                with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                    d = json.load(f)
                for cid, msgs in d.get("bot_messages", {}).items():
                    self.bot_messages[int(cid)] = deque(msgs, maxlen=MEMORY_BOT_MSGS)
                for cid, us in d.get("users", {}).items():
                    self.users[int(cid)] = deque(us, maxlen=MEMORY_USERS)
                for cid, cf in d.get("conflicts", {}).items():
                    self.conflicts[int(cid)] = deque(cf, maxlen=MEMORY_CONFLICTS)
                for cid, dialogs in d.get("dialogs", {}).items():
                    for uid, msgs in dialogs.items():
                        self.dialogs[int(cid)][int(uid)] = deque(msgs, maxlen=MEMORY_DIALOG)
                log.info("память загружена")
            except Exception as e:
                log.warning(f"память: {e}")
        if os.path.exists(REPLIED_FILE):
            try:
                with open(REPLIED_FILE, "r", encoding="utf-8") as f:
                    self.replied_ids = set(json.load(f))
                log.info(f"отвечено ранее: {len(self.replied_ids)}")
            except Exception as e:
                log.warning(f"replied: {e}")
        if os.path.exists(SEEN_FILE):
            try:
                with open(SEEN_FILE, "r", encoding="utf-8") as f:
                    d = json.load(f)
                total = 0
                for cid_str, ids in d.items():
                    cid = int(cid_str)
                    self.seen[cid] = set(ids)
                    self.seen_order[cid] = deque(ids[-MEMORY_SEEN:])
                    total += len(ids)
                log.info(f"виденных: {total}")
            except Exception as e:
                log.warning(f"seen: {e}")

    def save(self):
        try:
            d = {
                "bot_messages": {str(k): list(v) for k, v in self.bot_messages.items()},
                "users": {str(k): list(v) for k, v in self.users.items()},
                "conflicts": {str(k): list(v) for k, v in self.conflicts.items()},
                "dialogs": {str(k): {str(uid): list(msgs) for uid, msgs in v.items()}
                            for k, v in self.dialogs.items()},
                "chat_state": {str(cid): {
                    "counter": st.counter,
                    "num_to_msg": {str(k): v for k, v in st.num_to_msg.items()},
                    "num_to_user": {str(k): list(v) for k, v in st.num_to_user.items()},
                    "num_to_bot": {str(k): v for k, v in st.num_to_bot.items()},
                    "num_reply_to": {str(k): v for k, v in st.num_reply_to.items()},
                    "num_text": {str(k): v for k, v in st.num_text.items()},
                } for cid, st in chats.items()},
            }
            with open(MEMORY_FILE, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            with open(REPLIED_FILE, "w", encoding="utf-8") as f:
                json.dump(list(self.replied_ids)[-10000:], f)
            with open(SEEN_FILE, "w", encoding="utf-8") as f:
                json.dump({str(c): list(o) for c, o in self.seen_order.items()}, f)
        except Exception as e:
            log.error(f"save: {e}")

    def mark_seen(self, cid, mid):
        s = self.seen[cid]
        if mid in s: return False
        s.add(mid)
        o = self.seen_order[cid]
        o.append(mid)
        while len(o) > MEMORY_SEEN:
            s.discard(o.popleft())
        return True

    def was_seen(self, cid, mid): return mid in self.seen.get(cid, set())
    def mark_replied(self, mid): self.replied_ids.add(mid)
    def was_replied(self, mid): return mid in self.replied_ids
    def add_bot_msg(self, cid, mid, t): self.bot_messages[cid].append({"id": mid, "text": t})

    def is_reply_to_bot(self, cid, rid):
        for m in self.bot_messages[cid]:
            if m["id"] == rid: return m["text"]
        return None

    def add_user(self, cid, uid, name):
        for u in self.users[cid]:
            if u["id"] == uid: u["name"] = name; return
        self.users[cid].append({"id": uid, "name": name})

    def add_conflict(self, cid, uid, name):
        for c in self.conflicts[cid]:
            if c["user_id"] == uid: c["count"] = c.get("count", 0) + 1; return
        self.conflicts[cid].append({"user_id": uid, "name": name, "count": 1})

    def add_dialog(self, cid, uid, role, t): self.dialogs[cid][uid].append({"role": role, "text": t})

    def memory_text(self, cid, uid, reply_to=None, is_dm=False):
        parts = []
        if is_dm: parts.append("это ЛС")
        if reply_to: parts.append("тебе ответили на ТВОЁ: " + reply_to)
        d = self.dialogs[cid][uid]
        if d: parts.append("история:\n" + "\n".join(f"{m['role']}: {m['text']}" for m in d))
        my = list(self.bot_messages.get(cid, []))[-10:]
        if my: parts.append("твои последние:\n" + "\n".join(m["text"] for m in my))
        return "\n\n".join(parts) if parts else ""


memory = Memory()


def _extract(data):
    try:
        m = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return None
    if isinstance(m, dict):
        c = m.get("content")
        if c and isinstance(c, str) and c.strip():
            return c.strip()
    return None


def _try_model(model, msgs_text):
    url = ANYMODEL_BASE_URL + "/chat/completions"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": msgs_text},
        ],
        "temperature": 0.7,
        "stream": False,
        "max_tokens": 400,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + ANYMODEL_API_KEY,
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=AI_TIMEOUT) as r:
            d = json.loads(r.read().decode())
            t = _extract(d)
            if t: return t, None
            return None, "empty"
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return None, f"URLError {e.reason}"
    except Exception as e:
        return None, str(e)


def _ask_sync(msgs_text):
    t0 = time.time()
    for model in ANYMODEL_MODELS:
        if time.time() - t0 > MAX_AI_TOTAL:
            log.warning("[AI] бюджет исчерпан")
            break
        text, err = _try_model(model, msgs_text)
        if text:
            low = text.lower()
            if "решение" in low or "реши" in low:
                return text
            log.warning(f"[AI] {model} -> мусор: {text[:60]!r}")
            continue
        log.warning(f"[AI] {model} -> {err}")
    log.error("[AI] все модели недоступны")
    return None


async def ask_ai(msgs_text):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _ask_sync, msgs_text)


def parse_decision(text):
    if not text: return None, []
    num, messages, mode, current = None, [], None, []
    for line in text.split("\n"):
        low = line.strip().lower()
        if low.startswith("решение:"):
            v = line.split(":", 1)[1].strip()
            if v.lower() in ("нет", "no", "none", "-"): return None, []
            try: num = int(v.replace("#", "").strip())
            except: num = None
        elif low.startswith("сообщения:"):
            mode = "m"
        elif mode == "m":
            if line.strip() == "---":
                if current: messages.append(" ".join(current).strip()); current = []
            elif line.strip():
                current.append(line.strip())
    if current: messages.append(" ".join(current).strip())
    messages = [m for m in messages if m and m.lower() != "нет"]
    clean = []
    for m in messages:
        m = m.rstrip(".!?,;:")
        w = m.split()
        if len(w) > 15: m = " ".join(w[:15])
        m = m.replace(".", "").replace(",", "").replace("!", "").replace("?", "")
        clean.append(m)
    if num is None or not clean: return None, []
    return num, clean[:2]


class ChatState:
    def __init__(self, cid):
        self.chat_id = cid
        self.messages = deque(maxlen=HISTORY_SIZE)
        self.counter = 0
        self.num_to_msg, self.num_to_user, self.num_to_bot = {}, {}, {}
        self.num_reply_to, self.num_text = {}, {}
        self.needs_response = False
        self.force_reply_to = None
        self.last_msg_time = 0.0
        self.last_bot_reply_time = 0.0
        self.in_progress = False
        self.last_processed_id = None

    def add(self, sid, sname, t, mid, is_bot=False, reply_to_id=None):
        self.counter += 1
        n = self.counter
        self.messages.append((n, sname, t, mid))
        self.num_to_msg[n], self.num_to_user[n] = mid, (sid, sname)
        self.num_to_bot[n], self.num_reply_to[n] = is_bot, reply_to_id
        self.num_text[n] = t
        self.last_msg_time = time.time()
        while len(self.num_to_msg) > HISTORY_SIZE:
            old = min(self.num_to_msg.keys())
            self.num_to_msg.pop(old, None); self.num_to_user.pop(old, None)
            self.num_to_bot.pop(old, None); self.num_reply_to.pop(old, None)
            self.num_text.pop(old, None)
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
            mk = " <<< ОТВЕТЬ НА ЭТО" if n == tnum else ""
            lines.append(f"#{n} {s}{bn}: {t}{rn}{mk}")
        return "\n".join(lines)

    def trim_buffer(self, keep=HISTORY_SIZE):
        nums = sorted(self.num_to_msg.keys())
        if len(nums) <= keep: return
        for n in nums[:-keep]:
            self.num_to_msg.pop(n, None); self.num_to_user.pop(n, None)
            self.num_to_bot.pop(n, None); self.num_reply_to.pop(n, None)
            self.num_text.pop(n, None)

    def maybe_clear(self):
        self.trim_buffer(keep=HISTORY_SIZE)


chats = {}
client_global = None
entities_map = {}


async def send_messages(cid, state, tmid, tuid, tuname, messages):
    messages = [m for m in messages if not is_bad_response(m)]
    if not messages: return
    first = True
    for t in messages:
        sent = await client_global.send_message(cid, t, reply_to=tmid if first else None)
        memory.add_bot_msg(cid, sent.id, t)
        memory.add_dialog(cid, tuid, "ты", t)
        memory.mark_seen(cid, sent.id)
        state.add(0, MY_NAME, t, sent.id, is_bot=False)
        log.info(f"[{cid}] отправлено: {t[:60]}")
        first = False
        await asyncio.sleep(random.uniform(0.5, 1.5))
    state.last_bot_reply_time = time.time()
    memory.add_conflict(cid, tuid, tuname)
    memory.mark_replied(tmid)
    memory.save()


async def idle_checker():
    while True:
        try:
            now = time.time()
            for cid, st in chats.items():
                if st.needs_response or getattr(st, "in_progress", False): continue
                if not st.messages: continue
                ln = st.last_human_msg_num()
                if ln is None: continue
                lid = st.num_to_msg.get(ln)
                if lid is None or lid == st.last_processed_id: continue
                if memory.was_replied(lid): continue
                if now - st.last_msg_time < IDLE_TRIGGER_SEC: continue
                if now - st.last_bot_reply_time < IDLE_TRIGGER_SEC: continue
                st.needs_response = True
        except Exception as e:
            log.error(f"idle: {e}")
        await asyncio.sleep(3)


async def try_click_buttons(msg, cid):
    if not msg.buttons: return
    try:
        for ri, row in enumerate(msg.buttons):
            for ci, btn in enumerate(row):
                t = getattr(btn, "text", "") or "?"
                try:
                    await msg.click(ri, ci)
                    await asyncio.sleep(random.uniform(1.5, 3.5))
                except Exception:
                    pass
    except Exception:
        pass


async def process_message(msg, cid, me_id):
    if memory.was_seen(cid, msg.id): return
    memory.mark_seen(cid, msg.id)

    if cid not in chats: chats[cid] = ChatState(cid)
    st = chats[cid]

    sender = None
    try: sender = await msg.get_sender()
    except: pass

    un = getattr(sender, "username", "") if sender else ""
    sb = bool(getattr(sender, "bot", False)) if sender else False

    if msg.buttons and un not in NO_BUTTON_BOTS:
        asyncio.create_task(try_click_buttons(msg, cid))

    if msg.sender_id == me_id: return

    t = (msg.text or "").strip()
    if not t: return

    name = (getattr(sender, "first_name", None) or getattr(sender, "title", None) or "кто-то") if sender else "кто-то"
    uid = sender.id if sender and hasattr(sender, "id") else 0
    if name.lower() == MY_NAME.lower(): return

    rt = msg.reply_to_msg_id
    rtt = memory.is_reply_to_bot(cid, rt) if rt else None
    is_dm = cid > 0

    if sb or (un and un in IGNORED_BOTS):
        st.add(uid, name, t, msg.id, is_bot=True, reply_to_id=rt)
        return

    memory.add_user(cid, uid, name)
    memory.add_dialog(cid, uid, name, t)

    if is_dm:
        st.add(uid, name, t, msg.id, is_bot=False, reply_to_id=rt)
        st.needs_response = True
        memory.save()
        return

    if rtt:
        n = st.add(uid, name, t, msg.id, is_bot=False, reply_to_id=rt)
        st.force_reply_to = (n, rtt, uid, name)
        st.needs_response = True
        memory.save()
        return

    if is_skip_msg(t):
        memory.save()
        return

    if rt is not None:
        st.add(uid, name, t, msg.id, is_bot=False, reply_to_id=rt)
        memory.save()
        return

    st.add(uid, name, t, msg.id, is_bot=False, reply_to_id=rt)
    if st.counter >= TRIGGER_EVERY:
        st.needs_response = True
    memory.save()


last_ai_call = 0.0
ai_lock = asyncio.Lock()


async def worker():
    global last_ai_call
    while True:
        try:
            tgt = None
            for cid, st in chats.items():
                if st.needs_response and not getattr(st, "in_progress", False):
                    tgt = st; break
            if not tgt:
                await asyncio.sleep(0.1); continue
            async with ai_lock:
                st = tgt
                cid = st.chat_id
                st.needs_response = False
                st.in_progress = True
                if not st.messages:
                    st.in_progress = False; continue

                force = st.force_reply_to
                if force: num_f, rtt, uid, uname = force
                else: num_f, rtt, uid, uname = None, None, 0, "кто-то"

                tnum = st.last_human_msg_num()
                if tnum is None:
                    st.maybe_clear(); st.force_reply_to = None; st.in_progress = False
                    continue
                st.last_processed_id = st.num_to_msg.get(tnum)

                trt = st.num_reply_to.get(tnum)
                if trt is not None and num_f is None:
                    ok = any(m["id"] == trt for m in memory.bot_messages.get(cid, []))
                    if not ok:
                        st.force_reply_to = None; st.in_progress = False
                        st.last_msg_time = time.time(); continue

                ctx = st.context_text(tnum, limit=CONTEXT_SIZE)
                if not ctx.strip():
                    st.maybe_clear(); st.force_reply_to = None; st.in_progress = False
                    st.last_msg_time = time.time(); continue

                if not uid and st.num_to_user: uid, uname = st.num_to_user.get(tnum, (0, "кто-то"))
                mem = memory.memory_text(cid, uid, rtt, is_dm=cid > 0)
                bots = {st.num_to_user.get(n, ("?", "?"))[1] for n in st.num_to_msg if st.num_to_bot.get(n)}
                prompt = ""
                if cid > 0:
                    prompt += "это личное сообщение тебе. ответь.\n\n"
                else:
                    prompt += f"это чат. вот последние {len(ctx.splitlines())} сообщений.\n"
                    prompt += f"тебя зовут {MY_NAME}.\n"
                    if bots: prompt += "боты: " + ", ".join(b for b in bots if b) + "\n"
                    prompt += "в скобках кому отвечает человек, не путай.\n"
                    prompt += "ответь на последнее сообщение от человека.\n\n"
                prompt += f"сообщения:\n{ctx}\n"
                if mem: prompt += f"\n{mem}\n"

                t0 = time.time()
                ans = await ask_ai(prompt)
                last_ai_call = time.time()
                log.info(f"[{cid}] ИИ за {last_ai_call - t0:.1f}с")
                if not ans:
                    st.maybe_clear(); st.force_reply_to = None; st.in_progress = False
                    st.last_msg_time = time.time(); continue
                num_ai, messages = parse_decision(ans)
                if not messages:
                    st.maybe_clear(); st.force_reply_to = None; st.in_progress = False
                    st.last_msg_time = time.time(); continue
                if not st.num_to_msg:
                    st.maybe_clear(); st.force_reply_to = None; st.in_progress = False
                    st.last_msg_time = time.time(); continue
                if num_f is not None: num = num_f
                elif num_ai and num_ai in st.num_to_msg and not st.num_to_bot.get(num_ai):
                    num = num_ai
                else:
                    st.maybe_clear(); st.force_reply_to = None; st.in_progress = False
                    st.last_msg_time = time.time(); continue
                tmid = st.num_to_msg.get(num)
                if not tmid or memory.was_replied(tmid):
                    st.maybe_clear(); st.force_reply_to = None; st.in_progress = False
                    st.last_msg_time = time.time(); continue
                tuid, tuname = st.num_to_user.get(num, (uid, uname))
                log.info(f"[{cid}] отвечаю #{num} от {tuname}")
                await send_messages(cid, st, tmid, tuid, tuname, messages)
                st.maybe_clear(); st.force_reply_to = None; st.in_progress = False
        except Exception as e:
            log.error(f"worker: {e}")
            try:
                if 'st' in locals() and st: st.in_progress = False
            except: pass
            await asyncio.sleep(0.5)


async def poller():
    while True:
        try:
            me = await client_global.get_me()
            for cid, ent in list(entities_map.items()):
                try:
                    msgs = []
                    async for m in client_global.iter_messages(ent, limit=POLL_LIMIT):
                        msgs.append(m)
                    msgs.reverse()
                    for m in msgs: await process_message(m, cid, me.id)
                except Exception as e:
                    err = str(e).lower()
                    if "private" in err or "lack permission" in err or "banned" in err:
                        entities_map.pop(cid, None)
                    else:
                        log.error(f"poll {cid}: {e}")
        except Exception as e:
            log.error(f"poller: {e}")
        await asyncio.sleep(POLL_INTERVAL)


async def ensure_member(ent, name):
    joined = False
    try:
        await client_global(JoinChannelRequest(ent))
        joined = True
    except UserAlreadyParticipantError:
        joined = True
    except FloodWaitError as e:
        await asyncio.sleep(min(e.seconds, 30)); return False
    except Exception as e:
        log.warning(f"join {name}: {e}")
    try:
        full = await client_global(GetFullChannelRequest(ent))
        linked = getattr(full.full_chat, "linked_chat_id", None)
        if linked:
            try:
                le = await client_global.get_entity(linked)
                await client_global(JoinChannelRequest(le))
            except: pass
    except: pass
    return joined


async def resolve_chats(names):
    for name in names:
        ent = None
        try:
            if isinstance(name, int):
                ent = await client_global.get_entity(name)
            elif isinstance(name, str) and "t.me/+" in name:
                m = re.search(r"\+([A-Za-z0-9_-]+)", name)
                if m:
                    try:
                        upd = await client_global(ImportChatInviteRequest(m.group(1)))
                        ch = getattr(upd, "chats", [])
                        if ch: ent = ch[0]
                    except UserAlreadyParticipantError:
                        ent = await client_global.get_entity(name)
                    except: pass
            else:
                ent = await client_global.get_entity(name)
        except Exception as e:
            log.warning(f"нет {name}: {e}"); continue
        if ent:
            await ensure_member(ent, name)
            pid = utils.get_peer_id(ent)
            entities_map[pid] = ent
            log.info(f"слушаю {name} -> {pid}")


def restore_chat_state():
    if not os.path.exists(MEMORY_FILE): return
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        for cs, sd in d.get("chat_state", {}).items():
            cid = int(cs)
            st = ChatState(cid)
            st.counter = sd.get("counter", 0)
            st.num_to_msg = {int(k): v for k, v in sd.get("num_to_msg", {}).items()}
            st.num_to_user = {int(k): tuple(v) for k, v in sd.get("num_to_user", {}).items()}
            st.num_to_bot = {int(k): v for k, v in sd.get("num_to_bot", {}).items()}
            st.num_reply_to = {int(k): v for k, v in sd.get("num_reply_to", {}).items()}
            st.num_text = {int(k): v for k, v in sd.get("num_text", {}).items()}
            nums = sorted(st.num_to_msg.keys())
            if len(nums) > HISTORY_SIZE:
                for n in nums[:-HISTORY_SIZE]:
                    st.num_to_msg.pop(n, None); st.num_to_user.pop(n, None)
                    st.num_to_bot.pop(n, None); st.num_reply_to.pop(n, None)
                    st.num_text.pop(n, None)
            st.last_msg_time = time.time()
            st.last_bot_reply_time = time.time()
            chats[cid] = st
    except Exception as e:
        log.warning(f"restore: {e}")


async def preload_history(limit=HISTORY_SIZE):
    me = await client_global.get_me()
    for cid, ent in list(entities_map.items()):
        try:
            msgs = []
            async for m in client_global.iter_messages(ent, limit=limit):
                msgs.append(m)
            msgs.reverse()
            if cid not in chats: chats[cid] = ChatState(cid)
            st = chats[cid]
            for m in msgs:
                if m.sender_id == me.id: continue
                t = (m.text or "").strip()
                if not t: continue
                memory.mark_seen(cid, m.id)
                try: sender = await m.get_sender()
                except: sender = None
                if sender is None: continue
                name = getattr(sender, "first_name", None) or getattr(sender, "title", None) or "кто-то"
                uid = getattr(sender, "id", 0)
                sb = bool(getattr(sender, "bot", False))
                un = getattr(sender, "username", "") or ""
                st.add(uid, name, t, m.id, is_bot=sb or un in IGNORED_BOTS, reply_to_id=m.reply_to_msg_id)
                if not sb: memory.add_user(cid, uid, name)
            st.needs_response = False
            st.force_reply_to = None
            st.last_msg_time = time.time()
            st.last_bot_reply_time = time.time()
            log.info(f"preload {cid}: {len(st.messages)}")
        except Exception as e:
            log.warning(f"preload {cid}: {e}")
    memory.save()


async def main():
    global client_global
    if not TG_STRING_SESSION:
        log.error("TG_STRING_SESSION не задан")
        return
    if not API_ID or not API_HASH:
        log.error("API_ID / API_HASH не заданы")
        return
    log.info(f"имя: {MY_NAME}")
    log.info(f"моделей: {len(ANYMODEL_MODELS)}")
    log.info(f"чатов: {len(WATCH_CHATS)}")

    client_global = TelegramClient(StringSession(TG_STRING_SESSION), API_ID, API_HASH)
    await client_global.start()
    me = await client_global.get_me()
    log.info(f"запущен от {me.first_name} (id {me.id})")

    await resolve_chats(WATCH_CHATS)
    if not entities_map:
        log.error("нет чатов"); return
    log.info(f"слушаю {len(entities_map)} чатов")

    restore_chat_state()
    await preload_history()

    asyncio.create_task(worker())
    asyncio.create_task(idle_checker())
    asyncio.create_task(poller())
    log.info("воркеры запущены")

    @client_global.on(events.NewMessage(func=lambda e: e.is_private))
    async def dm_handler(event):
        try:
            me2 = await client_global.get_me()
            await process_message(event.message, event.chat_id, me2.id)
        except Exception as e:
            log.error(f"dm: {e}")

    await client_global.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
