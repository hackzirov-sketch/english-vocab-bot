import os, json, random, sqlite3, asyncio, threading, logging, io, math
from pathlib import Path
from datetime import date, datetime, timedelta
from collections import defaultdict

import httpx
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
    Message, CallbackQuery, InputFile, InlineQueryResultArticle,
    InputTextMessageContent,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

load_dotenv(Path(__file__).resolve().parent / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
DB_PATH = Path(__file__).resolve().parent / "database" / "master_maximal_v14_openrouter_ready.db"
if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN is missing in bot/.env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("vocab_bot")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
DB_LOCK, CONN = threading.Lock(), None
USER_DB, U_CONN = threading.Lock(), None

def db():
    global CONN
    if CONN is None:
        CONN = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        CONN.row_factory = sqlite3.Row; CONN.execute("PRAGMA journal_mode=WAL")
    return CONN
def q(sql, p=None):
    with DB_LOCK:
        c = db()
        return c.execute(sql, p).fetchall() if p else c.execute(sql).fetchall()
def q1(sql, p=None):
    with DB_LOCK:
        c = db()
        return c.execute(sql, p).fetchone() if p else c.execute(sql).fetchone()

def udb():
    global U_CONN
    if U_CONN is None:
        p = Path(__file__).resolve().parent / "user_data.db"
        U_CONN = sqlite3.connect(str(p), check_same_thread=False)
        U_CONN.row_factory = sqlite3.Row
        U_CONN.execute("""CREATE TABLE IF NOT EXISTS user_stats (
            user_id INTEGER PRIMARY KEY, total INTEGER DEFAULT 0, correct INTEGER DEFAULT 0,
            streak INTEGER DEFAULT 0, best_streak INTEGER DEFAULT 0, last_active TEXT,
            vocab_level INTEGER DEFAULT 1, xp INTEGER DEFAULT 0, chat_count INTEGER DEFAULT 0,
            daily_goal INTEGER DEFAULT 10, daily_done INTEGER DEFAULT 0, daily_date TEXT,
            level_test_score INTEGER DEFAULT 0, level_test_taken INTEGER DEFAULT 0)""")
        U_CONN.execute("""CREATE TABLE IF NOT EXISTS user_favorites (
            user_id INTEGER, word_id INTEGER, added_at TEXT DEFAULT (datetime('now')), PRIMARY KEY (user_id, word_id))""")
        U_CONN.execute("""CREATE TABLE IF NOT EXISTS user_notes (
            user_id INTEGER, word_id INTEGER, note TEXT, PRIMARY KEY (user_id, word_id))""")
        U_CONN.execute("""CREATE TABLE IF NOT EXISTS word_of_day (date TEXT PRIMARY KEY, word_id INTEGER)""")
        U_CONN.execute("""CREATE TABLE IF NOT EXISTS seen_words (
            user_id INTEGER, word_id INTEGER, seen_count INTEGER DEFAULT 1,
            correct_count INTEGER DEFAULT 0, wrong_count INTEGER DEFAULT 0,
            last_seen TEXT DEFAULT (datetime('now')), srs_level INTEGER DEFAULT 0,
            next_review TEXT, PRIMARY KEY (user_id, word_id))""")
        U_CONN.execute("""CREATE TABLE IF NOT EXISTS badges (
            user_id INTEGER, badge TEXT, earned_at TEXT DEFAULT (datetime('now')), PRIMARY KEY (user_id, badge))""")
        U_CONN.execute("""CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY, daily_reminder INTEGER DEFAULT 0, language TEXT DEFAULT 'uz',
            reminder_time TEXT DEFAULT '09:00', theme TEXT DEFAULT 'light', last_reminder_date TEXT DEFAULT NULL)""")
        U_CONN.execute("""CREATE TABLE IF NOT EXISTS user_activity (
            user_id INTEGER, date TEXT, actions INTEGER DEFAULT 1, PRIMARY KEY (user_id, date))""")
        U_CONN.execute("""CREATE TABLE IF NOT EXISTS daily_goals (
            user_id INTEGER, date TEXT, goal INTEGER DEFAULT 10, done INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, date))""")
        U_CONN.execute("""CREATE TABLE IF NOT EXISTS writing_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, text TEXT, feedback TEXT,
            score INTEGER, created_at TEXT DEFAULT (datetime('now')))""")
        U_CONN.execute("""CREATE TABLE IF NOT EXISTS topic_mastery (
            user_id INTEGER, topic TEXT, seen INTEGER DEFAULT 0, total INTEGER DEFAULT 0,
            correct_quiz INTEGER DEFAULT 0, total_quiz INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, topic))""")
        U_CONN.commit()
    return U_CONN
def uq(sql, p=None):
    with USER_DB:
        c = udb()
        return c.execute(sql, p).fetchall() if p else c.execute(sql).fetchall()
def uq1(sql, p=None):
    with USER_DB:
        c = udb()
        return c.execute(sql, p).fetchone() if p else c.execute(sql).fetchone()
def ux(sql, p=None):
    with USER_DB:
        c = udb()
        if p: c.execute(sql, p)
        else: c.execute(sql)
        c.commit()

# ===== CACHE =====
CACHE, CACHE_TTL = {}, 300
def cached(ttl=300):
    def deco(fn):
        def wrap(*a,**kw):
            k = fn.__name__
            n = datetime.now().timestamp()
            if k in CACHE and n-CACHE[k]["ts"]<ttl: return CACHE[k]["val"]
            v = fn(*a,**kw)
            CACHE[k]={"val":v,"ts":n}
            return v
        return wrap
    return deco
@cached()
def get_topics(): return [dict(r) for r in q("SELECT topic, COUNT(*) as count FROM vocab_enriched GROUP BY topic ORDER BY topic")]
@cached()
def word_count(): return q1("SELECT COUNT(*) FROM vocab_enriched")[0]
@cached()
def topic_count(): return q1("SELECT COUNT(*) FROM topics")[0]
@cached()
def quiz_count(): return q1("SELECT COUNT(*) FROM quiz_items")[0]
@cached()
def type_dist(): return q("SELECT type, COUNT(*) as count FROM vocab_enriched GROUP BY type ORDER BY count DESC")
@cached()
def get_levels():
    return [dict(r) for r in q("SELECT level, COUNT(*) as count FROM vocab_enriched GROUP BY level ORDER BY CASE level WHEN 'A1' THEN 1 WHEN 'A2' THEN 2 WHEN 'B1' THEN 3 WHEN 'B2' THEN 4 WHEN 'C1' THEN 5 ELSE 6 END")]

def get_wod():
    t = date.today().isoformat()
    r = uq1("SELECT word_id FROM word_of_day WHERE date=?", (t,))
    if r:
        row = q1("SELECT * FROM vocab_enriched WHERE id=?", (r["word_id"],))
        if row: return row
    row = q1("SELECT * FROM vocab_enriched ORDER BY RANDOM() LIMIT 1")
    if row: ux("INSERT OR REPLACE INTO word_of_day(date,word_id) VALUES(?,?)", (t, row["id"]))
    return row

def srs_next_review(srs_level):
    intervals = [0, 1, 3, 7, 14, 30, 60, 120, 240]
    if srs_level >= len(intervals): srs_level = len(intervals)-1
    return (date.today() + timedelta(days=intervals[srs_level])).isoformat()

def add_xp(uid, amount):
    ux("UPDATE user_stats SET xp=xp+? WHERE user_id=?", (amount, uid))
    stats = uq1("SELECT xp FROM user_stats WHERE user_id=?", (uid,))
    if stats: ux("UPDATE user_stats SET vocab_level=? WHERE user_id=?", (min(100, max(1, stats["xp"]//100+1)), uid))

def add_activity(uid):
    t = date.today().isoformat()
    ux("INSERT INTO user_activity(user_id,date) VALUES(?,?) ON CONFLICT(user_id,date) DO UPDATE SET actions=actions+1", (uid, t))
    ux("UPDATE user_stats SET last_active=? WHERE user_id=?", (t, uid))

def check_goal(uid):
    t = date.today().isoformat()
    g = uq1("SELECT * FROM daily_goals WHERE user_id=? AND date=?", (uid, t))
    s = uq1("SELECT daily_goal FROM user_stats WHERE user_id=?", (uid,))
    goal = s["daily_goal"] if s else 10
    done = g["done"] if g else 0
    return goal, done

def get_badges(uid):
    all_b = [(5,"📚 Yangi o'quvchi"),(25,"🎯 So'z ovchi"),(100,"⭐ Bilimdon"),
             (500,"🏆 Lug'at ustasi"),(1000,"👑 So'zlar qiroli"),(3000,"💎 Ensiklopediya"),
             (50,"🔥 50 kunlik seriya"),(7,"📅 Haftalik faol")]
    stats = uq1("SELECT * FROM user_stats WHERE user_id=?", (uid,))
    earned = {r["badge"] for r in uq("SELECT badge FROM badges WHERE user_id=?", (uid,))}
    new = []
    if stats:
        for count, name in all_b:
            if isinstance(count, int) and stats["total"] >= count and name not in earned:
                ux("INSERT INTO badges(user_id,badge) VALUES(?,?)", (uid, name)); new.append(name)
        if stats["best_streak"] >= 7 and "📅 Haftalik faol" not in earned:
            ux("INSERT INTO badges(user_id,badge) VALUES(?,?)", (uid, "📅 Haftalik faol")); new.append("📅 Haftalik faol")
        if stats["best_streak"] >= 50 and "🔥 50 kunlik seriya" not in earned:
            ux("INSERT INTO badges(user_id,badge) VALUES(?,?)", (uid, "🔥 50 kunlik seriya")); new.append("🔥 50 kunlik seriya")
    return new

async def gen_ai(prompt, temp=0.7, max_t=800):
    if not OPENROUTER_API_KEY: return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as cl:
            r = await cl.post(f"{OPENROUTER_BASE_URL}/chat/completions",
                headers={"Authorization":f"Bearer {OPENROUTER_API_KEY}","Content-Type":"application/json"},
                json={"model":OPENROUTER_MODEL,"messages":[{"role":"user","content":prompt}],
                      "temperature":temp,"max_tokens":max_t})
        if r.status_code != 200: return None
        data = r.json()
        choices = data.get("choices")
        if not choices: return None
        msg = choices[0].get("message") if isinstance(choices, list) and len(choices) > 0 else None
        if not msg or not msg.get("content"): return None
        return msg["content"].strip()
    except Exception as e: logger.error(f"AI: {e}"); return None

def _g(row, key, default=""):
    return row[key] if key in row and row[key] else default

def fmt_vocab(row, idx=None):
    p = f"<b>{idx}.</b> " if idx else ""
    return (f"{p}<b>{row['english']}</b>\n🇺🇿 {row['uzbek']}\n"
            f"📂 <code>{row['topic']}</code> | 🏷 <code>{row['type']}</code> | 📊 {row['level']}\n"
            f"📖 {_g(row,'definition')}\n📝 <i>{_g(row,'example_en')}</i>\n🇺🇿 {_g(row,'example_uz')}")

def fmt_ext(row):
    lines = [f"<b>📖 {row['english']}</b>", f"🇺🇿 <b>{row['uzbek']}</b>",
             f"📂 Topic: {row['topic']}", f"🏷 Type: {row['type']}", f"📊 Level: {row['level']}"]
    if "phonetic" in row and row["phonetic"]: lines.append(f"🔊 {row['phonetic']}")
    lines.extend([f"📖 {_g(row,'definition')}", f"🇬🇧 {_g(row,'example_en')}", f"🇺🇿 {_g(row,'example_uz')}"])
    return "\n".join(lines)

def parse_wrong(raw):
    try:
        p = json.loads(raw) if raw else []
        return [str(x) for x in p] if isinstance(p, list) else []
    except: return []

def fmt_grammar(p, title=None):
    lines = []
    if title: lines.append(f"<b>📚 {title}</b>\n")
    lines.append(f"<b>{p['title_en']}</b>\n🇺🇿 {p['title_uz']}\n")
    lines.append(f"<b>Level:</b> {p['level']} | <b>Category:</b> {p['category']}")
    if "ielts_part" in p and p["ielts_part"]: lines.append(f"<b>IELTS Part:</b> {p['ielts_part']}")
    lines.append(f"\n<b>Formula:</b>\n<code>{p['formula']}</code>\n")
    lines.append(f"<b>{p['meaning_uz']}</b>\n{_g(p,'explanation_uz')[:400] if _g(p,'explanation_uz') else ''}")
    if "when_to_use_uz" in p and p["when_to_use_uz"]: lines.append(f"\n<b>When to use:</b>\n{p['when_to_use_uz'][:300]}")
    ex = q("SELECT example_en,example_uz FROM grammar_examples WHERE pattern_id=? LIMIT 2", (p["id"],))
    if ex:
        lines.append("\n<b>Examples:</b>")
        for e in ex: lines.append(f"EN: {e['example_en']}\nUZ: {e['example_uz']}\n")
    return "\n".join(lines)

def mk():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📋 Mavzular"), KeyboardButton(text="📚 Lug'atlar"), KeyboardButton(text="🔍 Qidirish"), KeyboardButton(text="🎲 Random")],
        [KeyboardButton(text="❓ Test"), KeyboardButton(text="🃏 Flashcard"), KeyboardButton(text="🧩 Match"), KeyboardButton(text="🔄 Takrorlash")],
        [KeyboardButton(text="🤖 AI Jumla"), KeyboardButton(text="💬 AI Chat"), KeyboardButton(text="✍️ Writing")],
        [KeyboardButton(text="📊 Statistika"), KeyboardButton(text="🏅 Reyting"), KeyboardButton(text="🏆 Yutuqlar"), KeyboardButton(text="📅 Faoliyat")],
        [KeyboardButton(text="⭐ Sevimlilar"), KeyboardButton(text="📖 Kundagi so'z"), KeyboardButton(text="📥 Yuklash"), KeyboardButton(text="⚙️ Sozlamalar")],
        [KeyboardButton(text="📚 Grammar"), KeyboardButton(text="ℹ️ Yordam")],
    ], resize_keyboard=True)

def btn(t, d): return InlineKeyboardButton(text=t, callback_data=d)

def paginate(items, page, pp, prefix):
    s, e = page*pp, page*pp+pp
    chunk = items[s:e]
    btns = []
    for i, item in enumerate(chunk):
        label = item.get('topic', item.get('type', item.get('level', item.get('english', str(item)))))
        if 'count' in item: label = f"{label} ({item['count']})"
        btns.append([btn(label, f"{prefix}:{s+i}")])
    nav = []
    if page > 0: nav.append(btn("⬅️", f"{prefix}_pg:{page-1}"))
    if e < len(items): nav.append(btn("➡️", f"{prefix}_pg:{page+1}"))
    if nav: btns.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=btns)

def gkb(wid, is_fav=False):
    btns = [[btn("🤖 AI Jumla", f"gen:{wid}"), btn("📝 Note", f"note:{wid}")]]
    if is_fav: btns[0].append(btn("💔", f"unfav:{wid}"))
    else: btns[0].append(btn("⭐", f"fav:{wid}"))
    return InlineKeyboardMarkup(inline_keyboard=btns)

def mkb(wid):
    modes = [("☀️ Daily","daily"),("🎤 Speaking","speaking"),("✍️ Writing","writing"),
             ("📄 Essay","essay"),("💼 Formal","formal")]
    return InlineKeyboardMarkup(inline_keyboard=[[btn(l,f"mode:{wid}:{m}")] for l,m in modes])

def qkb(qid, opts, correct):
    return InlineKeyboardMarkup(inline_keyboard=[[btn(o[:40], f"qa:{qid}:{i}")] for i,o in enumerate(opts)])

# ===================== HANDLERS =====================
@dp.message(CommandStart())
async def start(m: Message):
    uid = m.from_user.id
    ux("INSERT OR IGNORE INTO user_stats(user_id) VALUES(?)", (uid,))
    ux("INSERT OR IGNORE INTO user_settings(user_id) VALUES(?)", (uid,))
    await m.answer(
        "<b>📚 English Vocabulary Master</b>\n\n"
        f"3480+ so'z, 24 mavzu, 32 grammar pattern\n"
        f"AI jumla, AI Chat, testlar, yutuqlar\n\n"
        f"👇 Tugmalardan foydalaning:", reply_markup=mk())

@dp.message(Command("help"))
@dp.message(F.text == "ℹ️ Yordam")
async def help_(m: Message):
    await m.answer(
        "<b>📋 Barcha buyruqlar:</b>\n\n"
        "<b>📚 So'zlar:</b>\n"
        "/topics — Mavzular\n/types — Turlar\n/all_words — Barcha lug'atlar\n/search <w> — Qidirish\n"
        "/topic <MAVZU> — Mavzu bo'yicha\n/random — Tasodifiy so'z\n"
        "/word — Kundagi so'z\n\n"
        "<b>❓ Testlar:</b>\n"
        "/quiz — Tasodifiy test\n/custom_quiz — Mavzu bo'yicha 10 ta\n"
        "/level_test — Daraja aniqlash (20 savol)\n/quiz_by_level <A1-C1> — Daraja bo'yicha\n\n"
        "<b>🎮 O'qish:</b>\n"
        "/flashcard — Flashcard rejimi\n/match — So'zlar o'yini\n"
        "/review — Takrorlash (SRS)\n\n"
        "<b>🤖 AI:</b>\n/gen <w> — AI jumla\n/chat — AI suhbat\n"
        "/writing — Writing practice\n/check_grammar <gap> — Grammatikani tekshirish\n\n"
        "<b>📊 Shaxsiy:</b>\n"
        "/stats — Statistika\n/leaderboard — Reyting\n/badges — Yutuqlar\n"
        "/fav — Sevimlilar\n/goal — Kunlik maqsad\n"
        "/activity — Faoliyat kalendari\n"
        "/word — Kundagi so'z\n"
        "/export — Sevimlilarni yuklash\n/settings — Sozlamalar\n\n"
        "<b>📚 Grammar:</b>\n"
        "/grammar — Grammar sections\n/grammar_browse — Barcha patterns\n"
        "/grammar_part1, /grammar_part2, /grammar_part3\n/grammar_quiz", reply_markup=mk())

# ===== TOPICS =====
@dp.message(Command("topics"))
@dp.message(F.text == "📋 Mavzular")
async def topics(m: Message):
    try:
        t = get_topics()
        await m.answer(f"<b>📂 Mavzular</b> — {len(t)} ta, {word_count()} so'z", reply_markup=paginate(t, 0, 10, "tp"))
    except Exception as e: await m.answer(f"❌ {e}")

@dp.callback_query(F.data.startswith("tp_pg:"))
async def tp_page(c: CallbackQuery):
    page = int(c.data.split(":")[1])
    await c.message.edit_reply_markup(reply_markup=paginate(get_topics(), page, 10, "tp"))
    await c.answer()

TOPIC_PP = 10
@dp.callback_query(F.data.startswith("tp:"))
async def tp_cb(c: CallbackQuery):
    idx = int(c.data.split(":")[1])
    topics = get_topics()
    if idx >= len(topics): return await c.answer("❌", show_alert=True)
    topic = topics[idx]["topic"]
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=None)
    total = q1("SELECT COUNT(*) FROM vocab_enriched WHERE topic=?", (topic,))[0]
    if total == 0: return await c.message.answer(f"❌ <code>{topic}</code> bo'sh.")
    uid = c.from_user.id
    seen = uq1("SELECT COUNT(*) as c FROM seen_words sw JOIN vocab_enriched v ON v.id=sw.word_id WHERE sw.user_id=? AND v.topic=?", (uid, topic))
    pct_str = f" ({seen['c']}/{total} ko'rgan)" if seen and total else ""
    rows = q(f"SELECT id, english, uzbek FROM vocab_enriched WHERE topic=? ORDER BY english LIMIT {TOPIC_PP} OFFSET 0", (topic,))
    btns = []
    for i, r in enumerate(rows):
        btns.append([btn(f"{i+1}. {r['english']} — {r['uzbek']}", f"tp_word:{r['id']}")])
    nav = []
    if TOPIC_PP < total: nav.append(btn("➡️", f"tp_w:{idx}:1"))
    if nav: btns.append(nav)
    await c.message.answer(f"📂 <b>{topic}</b>{pct_str}\n1-{len(rows)} / {total}:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@dp.callback_query(F.data.startswith("tp_w:"))
async def tp_word_page(c: CallbackQuery):
    p = c.data.split(":")
    tidx, page = int(p[1]), int(p[2])
    topics = get_topics()
    if tidx >= len(topics): return await c.answer("❌", show_alert=True)
    topic = topics[tidx]["topic"]
    total = q1("SELECT COUNT(*) FROM vocab_enriched WHERE topic=?", (topic,))[0]
    offset = page * TOPIC_PP
    rows = q(f"SELECT id, english, uzbek FROM vocab_enriched WHERE topic=? ORDER BY english LIMIT {TOPIC_PP} OFFSET {offset}", (topic,))
    if not rows: return await c.answer("❌", show_alert=True)
    btns = []
    for i, r in enumerate(rows):
        btns.append([btn(f"{offset+i+1}. {r['english']} — {r['uzbek']}", f"tp_word:{r['id']}")])
    nav = []
    if page > 0: nav.append(btn("⬅️", f"tp_w:{tidx}:{page-1}"))
    if offset + TOPIC_PP < total: nav.append(btn("➡️", f"tp_w:{tidx}:{page+1}"))
    if nav: btns.append(nav)
    start = offset + 1
    end = min(offset + len(rows), total)
    await c.message.edit_text(f"📂 <b>{topic}</b>\n{start}-{end} / {total}:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    await c.answer()

@dp.callback_query(F.data.startswith("tp_word:"))
async def tp_word_detail(c: CallbackQuery):
    wid = int(c.data.split(":")[1])
    row = q1("SELECT * FROM vocab_enriched WHERE id=?", (wid,))
    if not row: return await c.answer("❌", show_alert=True)
    uid = c.from_user.id
    fav = uq1("SELECT 1 FROM user_favorites WHERE user_id=? AND word_id=?", (uid, wid))
    await c.message.answer(fmt_ext(row), reply_markup=gkb(wid, fav))
    ux("INSERT INTO seen_words(user_id,word_id,seen_count) VALUES(?,?,1) ON CONFLICT(user_id,word_id) DO UPDATE SET seen_count=seen_count+1,last_seen=datetime('now')", (uid, wid))
    topic = row["topic"] if "topic" in row else ""
    if topic:
        total_t = q1("SELECT COUNT(*) FROM vocab_enriched WHERE topic=?", (topic,))[0]
        ux("INSERT INTO topic_mastery(user_id,topic,seen,total) VALUES(?,?,1,?) ON CONFLICT(user_id,topic) DO UPDATE SET seen=seen+1,total=?", (uid, topic, total_t, total_t))
    await c.answer()

@dp.message(Command("topic"))
async def topic_cmd(m: Message):
    p = m.text.split(maxsplit=1)
    if len(p) < 2: return await m.answer("📂 /topic <MAVZU>\nMasalan: /topic Education")
    t = p[1].strip().upper()
    try:
        rows = q("SELECT * FROM vocab_enriched WHERE topic=? ORDER BY RANDOM() LIMIT 10", (t,))
        if not rows: return await m.answer(f"❌ <code>{t}</code> bo'yicha so'z yo'q.")
        await m.answer(f"📂 <b>{t}</b> — {len(rows)} ta:")
        uid = m.from_user.id
        for i, r in enumerate(rows, 1):
            fav = uq1("SELECT 1 FROM user_favorites WHERE user_id=? AND word_id=?", (uid, r["id"]))
            await m.answer(fmt_vocab(r, i), reply_markup=gkb(r["id"], fav))
            ux("INSERT INTO seen_words(user_id,word_id,seen_count) VALUES(?,?,1) ON CONFLICT(user_id,word_id) DO UPDATE SET seen_count=seen_count+1,last_seen=datetime('now')", (uid, r["id"]))
    except Exception as e: await m.answer(f"❌ {e}")

# ===== RANDOM =====
@dp.message(Command("random"))
@dp.message(F.text == "🎲 Random")
async def random_(m: Message):
    try:
        row = q1("SELECT * FROM vocab_enriched ORDER BY RANDOM() LIMIT 1")
        if not row: return await m.answer("❌ So'z topilmadi.")
        uid = m.from_user.id
        fav = uq1("SELECT 1 FROM user_favorites WHERE user_id=? AND word_id=?", (uid, row["id"]))
        await m.answer(f"<b>🎲 Tasodifiy so'z</b>\n\n{fmt_ext(row)}", reply_markup=gkb(row["id"], fav))
        add_xp(uid, 1); add_activity(uid)
    except Exception as e: await m.answer(f"❌ {e}")

# ===== ALL WORDS =====
ALL_PP = 10
@dp.message(Command("all_words"))
@dp.message(F.text == "📚 Lug'atlar")
async def all_words(m: Message):
    try:
        total = word_count()
        rows = q(f"SELECT id, english, uzbek FROM vocab_enriched ORDER BY english LIMIT {ALL_PP} OFFSET 0")
        if not rows: return await m.answer("❌ So'z topilmadi.")
        btns = []
        for i, r in enumerate(rows):
            btns.append([btn(f"{i+1}. {r['english']} — {r['uzbek']}", f"dict_w:{r['id']}")])
        nav = [btn(f"➡️ {ALL_PP+1}-{min(ALL_PP*2,total)}", "dict_pg:1")]
        btns.append(nav)
        await m.answer(f"<b>📚 Barcha lug'atlar</b> ({total} ta)\n1-{len(rows)}:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    except Exception as e: await m.answer(f"❌ {e}")

@dp.callback_query(F.data.startswith("dict_pg:"))
async def dict_page(c: CallbackQuery):
    page = int(c.data.split(":")[1])
    offset = page * ALL_PP
    try:
        total = word_count()
        rows = q(f"SELECT id, english, uzbek FROM vocab_enriched ORDER BY english LIMIT {ALL_PP} OFFSET {offset}")
        if not rows: return await c.answer("❌", show_alert=True)
        btns = []
        for i, r in enumerate(rows):
            btns.append([btn(f"{offset+i+1}. {r['english']} — {r['uzbek']}", f"dict_w:{r['id']}")])
        nav = []
        if page > 0: nav.append(btn(f"⬅️ {offset-ALL_PP+1}-{offset}", f"dict_pg:{page-1}"))
        if offset + ALL_PP < total:
            n_end = min(offset + ALL_PP*2, total)
            nav.append(btn(f"➡️ {offset+ALL_PP+1}-{n_end}", f"dict_pg:{page+1}"))
        if nav: btns.append(nav)
        start = offset + 1
        end = min(offset + len(rows), total)
        await c.message.edit_text(f"<b>📚 Barcha lug'atlar</b> ({total} ta)\n{start}-{end}:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
        await c.answer()
    except Exception as e: await c.answer(f"❌ {e}", show_alert=True)

@dp.callback_query(F.data.startswith("dict_w:"))
async def dict_word(c: CallbackQuery):
    wid = int(c.data.split(":")[1])
    try:
        row = q1("SELECT * FROM vocab_enriched WHERE id=?", (wid,))
        if not row: return await c.answer("❌", show_alert=True)
        uid = c.from_user.id
        fav = uq1("SELECT 1 FROM user_favorites WHERE user_id=? AND word_id=?", (uid, wid))
        await c.message.answer(fmt_ext(row), reply_markup=gkb(wid, fav))
        await c.answer()
    except Exception as e: await c.answer(f"❌ {e}", show_alert=True)

# ===== TYPES =====
@dp.message(Command("types"))
@dp.message(F.text == "🏷 Turlar")
async def types_(m: Message):
    try:
        rows = type_dist()
        await m.answer("<b>🏷 So'z turlari:</b>\n"+"\n".join(f"  • <code>{r['type']}</code> — {r['count']} ta" for r in rows), reply_markup=mk())
    except Exception as e: await m.answer(f"❌ {e}")

# ===== SEARCH =====
@dp.message(F.text == "🔍 Qidirish")
async def sp(m: Message): await m.answer("🔍 So'z kiriting:\n<code>abandon</code>")
@dp.message(Command("search"))
async def search_(m: Message):
    p = m.text.split(maxsplit=1)
    if len(p) < 2: return await m.answer("🔍 /search <so'z>")
    q = p[1].strip()
    try:
        pat = f"%{q}%"
        rows = q("SELECT * FROM vocab_enriched WHERE english LIKE ? OR uzbek LIKE ? OR definition LIKE ? "
                 "ORDER BY CASE WHEN english LIKE ? THEN 0 WHEN uzbek LIKE ? THEN 1 ELSE 2 END LIMIT 8", (pat, pat, pat, q, q))
        if not rows: return await m.answer(f"❌ '{q}' bo'yicha topilmadi.")
        await m.answer(f"🔍 <b>{q}</b> — {len(rows)} ta:")
        uid = m.from_user.id
        for i, r in enumerate(rows, 1):
            fav = uq1("SELECT 1 FROM user_favorites WHERE user_id=? AND word_id=?", (uid, r["id"]))
            await m.answer(("⭐ " if fav else "")+fmt_vocab(r, i), reply_markup=gkb(r["id"], fav))
    except Exception as e: await m.answer(f"❌ {e}")

# ===== AI GEN =====
@dp.message(F.text == "🤖 AI Jumla")
async def gp(m: Message): await m.answer("🤖 So'z kiriting:\n<code>abandon</code>")
@dp.message(Command("gen"))
async def gen_(m: Message):
    p = m.text.split(maxsplit=1)
    if len(p) < 2: return await m.answer("🤖 /gen <so'z>")
    q = p[1].strip()
    try:
        row = q1("SELECT * FROM vocab_enriched WHERE english=? OR english LIKE ? LIMIT 1", (q, f"%{q}%"))
        if not row: return await m.answer(f"❌ '<code>{q}</code>' topilmadi.")
        msg = await m.answer("⏳ AI yaratmoqda...")
        r = await gen_ai(f"""Create ONE sentence using "{row['english']}" ({row['uzbek']}). Topic: {row['topic']}. Level: {row['level']}.
Return JSON: {{"sentence_en":"...","sentence_uz":"...","explanation_uz":"...","word":"{row['english']}"}}""")
        if not r: return await msg.edit_text("❌ AI xatosi.")
        if r.startswith("```"): r = r.split("\n",1)[1] if "\n" in r else r[3:]
        if r.endswith("```"): r = r.rsplit("```",1)[0]
        res = json.loads(r.strip())
        await msg.edit_text(f"<b>🤖 AI Generated</b>\n\n<b>🇬🇧</b> {res.get('sentence_en','')}\n<b>🇺🇿</b> {res.get('sentence_uz','')}\n\n<b>💡</b> {res.get('explanation_uz','')}")
        add_xp(m.from_user.id, 2)
    except Exception as e: await m.answer(f"❌ {e}")

# ===== AI CHAT =====
chat_sessions = {}
@dp.message(Command("chat"))
@dp.message(F.text == "💬 AI Chat")
async def chat_(m: Message):
    uid = m.from_user.id
    chat_sessions[uid] = [{"role":"system","content":"You are an English tutor for Uzbek students. Practice English conversation. Correct mistakes gently, explain in Uzbek when needed. Max 200 words per response."}]
    await m.answer("💬 <b>AI Chat</b> — Ingliz tilida suhbatlashing!\nXatolarni tuzataman va tushuntiraman.\n/chat_end — tugatish", reply_markup=mk())
@dp.message(Command("chat_end"))
async def chat_end(m: Message):
    chat_sessions.pop(m.from_user.id, None)
    await m.answer("✅ Suhbat tugadi. /chat bilan qayta boshlang.")

# ===== WRITING =====
@dp.message(Command("writing"))
@dp.message(F.text == "✍️ Writing")
async def writing_(m: Message):
    await m.answer(
        "✍️ <b>Writing Practice</b>\n\n"
        "Ingliz tilida matn yozing (5-10 jumla), "
        "AI xatolarni tuzatadi, baholaydi va maslahat beradi.\n\n"
        "Masalan: <code>Last weekend I go to park with my friends. We play football and have fun.</code>")

# ===== FLASHCARD =====
@dp.message(Command("flashcard"))
@dp.message(F.text == "🃏 Flashcard")
async def fc_(m: Message):
    uid = m.from_user.id
    try:
        row = q1("SELECT * FROM vocab_enriched ORDER BY RANDOM() LIMIT 1")
        if not row: return await m.answer("❌ So'z topilmadi.")
        await m.answer(
            f"🃏 <b>Flashcard</b>\n\n<b>{row['english']}</b>\n\nMa'nosini o'ylab ko'ring, so'ng pastdagi tugmani bosing.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [btn("👁 Javobni ko'rish", f"fc_reveal:{row['id']}"), btn("➡️ Keyingi", "fc_next")]
            ]))
        add_activity(uid)
    except Exception as e: await m.answer(f"❌ {e}")

@dp.callback_query(F.data.startswith("fc_reveal:"))
async def fc_reveal(c: CallbackQuery):
    wid = int(c.data.split(":")[1])
    row = q1("SELECT * FROM vocab_enriched WHERE id=?", (wid,))
    if not row: return await c.answer("❌", show_alert=True)
    await c.message.edit_text(
        f"🃏 <b>Flashcard</b>\n\n<b>{row['english']}</b>\n\n🇺🇿 {row['uzbek']}\n📖 {_g(row,'definition')}\n📝 {_g(row,'example_en')}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [btn("✅ Bilar edim", f"fc_easy:{wid}"), btn("❌ Bilmasdim", f"fc_hard:{wid}")],
            [btn("🔄 Keyingi", "fc_next")]
        ]))
    await c.answer()

@dp.callback_query(F.data == "fc_next")
async def fc_next(c: CallbackQuery):
    row = q1("SELECT * FROM vocab_enriched ORDER BY RANDOM() LIMIT 1")
    if not row: return await c.answer("❌", show_alert=True)
    await c.message.edit_text(
        f"🃏 <b>Flashcard</b>\n\n<b>{row['english']}</b>\n\nMa'nosini o'ylab ko'ring...",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [btn("👁 Javob", f"fc_reveal:{row['id']}"), btn("➡️ Keyingi", "fc_next")]
        ]))
    await c.answer()

@dp.callback_query(F.data.startswith("fc_easy:"))
async def fc_easy(c: CallbackQuery):
    wid = int(c.data.split(":")[1])
    ux("INSERT INTO seen_words(user_id,word_id,correct_count,srs_level,next_review) VALUES(?,?,1,1,?) "
       "ON CONFLICT(user_id,word_id) DO UPDATE SET correct_count=correct_count+1,srs_level=MIN(srs_level+1,8),next_review=?",
       (c.from_user.id, wid, srs_next_review(1), srs_next_review(1)))
    await c.answer("✅ Zo'r! Keyingi safar shu so'z uzoqroq vaqtdan keyin keladi.", show_alert=False)
    await fc_next(c)

@dp.callback_query(F.data.startswith("fc_hard:"))
async def fc_hard(c: CallbackQuery):
    wid = int(c.data.split(":")[1])
    ux("INSERT INTO seen_words(user_id,word_id,wrong_count,srs_level,next_review) VALUES(?,?,1,0,?) "
       "ON CONFLICT(user_id,word_id) DO UPDATE SET wrong_count=wrong_count+1,srs_level=0,next_review=date('now')",
       (c.from_user.id, wid, date.today().isoformat()))
    await c.answer("🔁 Eslab qoling! Tez orada yana ko'ramiz.", show_alert=False)
    await fc_next(c)

# ===== MATCH GAME =====
match_games = {}
@dp.message(Command("match"))
@dp.message(F.text == "🧩 Match")
async def match_(m: Message):
    uid = m.from_user.id
    try:
        rows = q("SELECT * FROM vocab_enriched ORDER BY RANDOM() LIMIT 4")
        if len(rows) < 4: return await m.answer("❌ Yetarli so'z yo'q.")
        pairs = [(r["english"], r["uzbek"]) for r in rows]
        match_games[uid] = {"pairs": pairs, "found": set(), "attempts": 0, "start": datetime.now()}
        items = []
        for en, uz in pairs:
            items.append((en, "en"))
            items.append((uz, "uz"))
        random.shuffle(items)
        txt = "🧩 <b>Match Game</b>\n\nIngliz va Uzbek juftliklarni toping:\n\n"
        for i, (word, lang) in enumerate(items):
            icon = "🇬🇧" if lang == "en" else "🇺🇿"
            txt += f"{i+1}. {icon} {word}\n"
        match_games[uid]["items"] = items
        await m.answer(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [btn(f"{i+1}", f"match:{i}") for i in range(4)],
            [btn(f"{i+1}", f"match:{i}") for i in range(4,8)],
            [btn("🔄 Yangi", "match_new")]
        ]))
    except Exception as e: await m.answer(f"❌ {e}")

async def match_next(m: Message, uid):
    try:
        rows = q("SELECT * FROM vocab_enriched ORDER BY RANDOM() LIMIT 4")
        if len(rows) < 4: return await m.answer("❌ Yetarli so'z yo'q.")
        pairs = [(r["english"], r["uzbek"]) for r in rows]
        match_games[uid] = {"pairs": pairs, "found": set(), "attempts": 0, "start": datetime.now()}
        items = []
        for en, uz in pairs:
            items.append((en, "en"))
            items.append((uz, "uz"))
        random.shuffle(items)
        txt = "🧩 <b>Match Game</b>\n\nIngliz va Uzbek juftliklarni toping:\n\n"
        for i, (word, lang) in enumerate(items):
            icon = "🇬🇧" if lang == "en" else "🇺🇿"
            txt += f"{i+1}. {icon} {word}\n"
        match_games[uid]["items"] = items
        await m.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [btn(f"{i+1}", f"match:{i}") for i in range(4)],
            [btn(f"{i+1}", f"match:{i}") for i in range(4,8)],
            [btn("🔄 Yangi", "match_new")]
        ]))
    except Exception as e: await m.answer(f"❌ {e}")

@dp.callback_query(F.data == "match_new")
async def match_new(c: CallbackQuery):
    match_games.pop(c.from_user.id, None)
    match_sel.pop(c.from_user.id, None)
    await match_next(c.message, c.from_user.id)

match_sel = {}
@dp.callback_query(F.data.startswith("match:"))
async def match_cb(c: CallbackQuery):
    uid = c.from_user.id
    idx = int(c.data.split(":")[1])
    game = match_games.get(uid)
    if not game: return await c.answer("❌ O'yin topilmadi. /match", show_alert=True)
    items = game["items"]
    if idx < 0 or idx >= len(items): return await c.answer("❌", show_alert=True)
    if uid not in match_sel: match_sel[uid] = []
    sel = match_sel[uid]
    sel.append(idx)
    if len(sel) == 2:
        i1, i2 = sel
        w1, l1 = items[i1]
        w2, l2 = items[i2]
        match_sel[uid] = []
        game["attempts"] += 1
        if l1 != l2:
            # Find pair
            for en, uz in game["pairs"]:
                if (w1 == en and w2 == uz) or (w1 == uz and w2 == en):
                    game["found"].add(en)
                    await c.answer("✅ To'g'ri!", show_alert=False)
                    if len(game["found"]) == len(game["pairs"]):
                        elapsed = (datetime.now() - game["start"]).seconds
                        score = max(100 - game["attempts"] * 5, 10)
                        await c.message.edit_text(
                            f"🎉 <b>Tabriklaymiz!</b>\n\n"
                            f"Barcha {len(game['pairs'])} juftlik topildi!\n"
                            f"📊 Urinishlar: {game['attempts']}\n"
                            f"⏱ Vaqt: {elapsed} soniya\n"
                            f"🏆 Ball: {score}")
                        add_xp(uid, score)
                        match_games.pop(uid, None)
                        match_sel.pop(uid, None)
                    return
        await c.answer("❌ Noto'g'ri, qayta urinib ko'ring", show_alert=False)
    else:
        await c.answer(f"1-tanlandi. Yana birini tanlang.", show_alert=False)

# match_new moved above

# ===== QUIZ =====
@dp.message(Command("quiz"))
@dp.message(F.text == "❓ Test")
async def quiz_(m: Message):
    p = m.text.split(maxsplit=1)
    topic = p[1].strip().upper() if len(p)>1 and not p[1].startswith("/") else None
    try:
        row = q1("SELECT * FROM quiz_items ORDER BY RANDOM() LIMIT 1") if not topic else \
              q1("SELECT * FROM quiz_items WHERE topic=? ORDER BY RANDOM() LIMIT 1", (topic,))
        if not row: return await m.answer("❌ Test topilmadi.")
        correct = row["correct_answer"]
        opts = [correct] + parse_wrong(row["wrong_answers_json"])[:3]
        random.shuffle(opts)
        await m.answer(f"<b>❓ Savol:</b>\n{row['question']}", reply_markup=qkb(row["id"], opts, correct))
    except Exception as e: await m.answer(f"❌ {e}")

# ===== QUIZ BY LEVEL =====
@dp.message(Command("quiz_by_level"))
async def qbl_(m: Message):
    p = m.text.split(maxsplit=1)
    if len(p) < 2: return await m.answer("🎯 /quiz_by_level <A1|A2|B1|B2|C1>\nMasalan: /quiz_by_level B1")
    level = p[1].strip().upper()
    try:
        row = q1("SELECT qi.* FROM quiz_items qi JOIN vocab_enriched v ON v.id=qi.vocab_id WHERE v.level=? ORDER BY RANDOM() LIMIT 1", (level,))
        if not row: return await m.answer(f"❌ <code>{level}</code> darajasida test yo'q.")
        correct = row["correct_answer"]
        opts = [correct] + parse_wrong(row["wrong_answers_json"])[:3]
        random.shuffle(opts)
        await m.answer(f"<b>🎯 {level} Test</b>\n\n{row['question']}", reply_markup=qkb(row["id"], opts, correct))
    except Exception as e: await m.answer(f"❌ {e}")

# ===== CUSTOM QUIZ =====
@dp.message(Command("custom_quiz"))
@dp.message(F.text == "📝 Maxsus test")
async def cq_start(m: Message):
    await m.answer("📝 <b>Maxsus test</b>\nMavzuni tanlang:", reply_markup=paginate(get_topics(), 0, 10, "cq"))

@dp.callback_query(F.data.startswith("cq_pg:"))
async def cq_page(c: CallbackQuery):
    await c.message.edit_reply_markup(reply_markup=paginate(get_topics(), int(c.data.split(":")[1]), 10, "cq"))
    await c.answer()

@dp.callback_query(F.data.startswith("cq:"))
async def cq_topic(c: CallbackQuery):
    idx = int(c.data.split(":")[1])
    topics = get_topics()
    if idx >= len(topics): return await c.answer("❌", show_alert=True)
    topic = topics[idx]["topic"]
    await c.answer()
    rows = q("SELECT * FROM quiz_items WHERE topic=? ORDER BY RANDOM() LIMIT 10", (topic,))
    if not rows: return await c.message.answer(f"❌ <code>{topic}</code> bo'yicha test yo'q.")
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer(f"📝 <b>{topic}</b> — 10 ta test:")
    for r in rows:
        correct = r["correct_answer"]
        opts = [correct] + parse_wrong(r["wrong_answers_json"])[:3]
        random.shuffle(opts)
        await c.message.answer(f"<b>❓</b> {r['question']}", reply_markup=qkb(r["id"], opts, correct))

# ===== LEVEL TEST =====
level_tests = {}
@dp.message(Command("level_test"))
@dp.message(F.text == "🎯 Daraja test")
async def lt_start(m: Message):
    uid = m.from_user.id
    rows = q("SELECT * FROM quiz_items ORDER BY RANDOM() LIMIT 20")
    if len(rows) < 20:
        rows = q("SELECT * FROM quiz_items ORDER BY RANDOM() LIMIT 10")
    level_tests[uid] = {"rows": list(rows), "idx": 0, "correct": 0}
    await m.answer(f"🎯 <b>Daraja testi</b>\n\n{len(rows)} ta savol. Boshlaymizmi?",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn("✅ Boshlash", "lt_go")]]))

@dp.callback_query(F.data == "lt_go")
async def lt_go(c: CallbackQuery):
    uid = c.from_user.id
    test = level_tests.get(uid)
    if not test or test["idx"] >= len(test["rows"]): return await c.answer("❌", show_alert=True)
    await lt_next(c.message, uid)
    await c.answer()

async def lt_next(msg, uid, edit=False):
    test = level_tests.get(uid)
    if not test or test["idx"] >= len(test["rows"]):
        score = test["correct"] if test else 0
        total = len(test["rows"]) if test else 0
        pct = score/total*100 if total > 0 else 0
        levels = [(90,"C1"),(75,"B2"),(55,"B1"),(35,"A2"),(0,"A1")]
        for threshold, lvl in levels:
            if pct >= threshold: level = lvl; break
        ux("UPDATE user_stats SET level_test_score=?,level_test_taken=1 WHERE user_id=?", (score, uid))
        await msg.edit_text(f"🎯 <b>Test yakunlandi!</b>\n\nTo'g'ri: {score}/{total} ({pct:.0f}%)\nDarajangiz: <b>{level}</b>\n\nA1 boshlang'ich — C1 yuqori")
        level_tests.pop(uid, None)
        return
    row = test["rows"][test["idx"]]
    correct = row["correct_answer"]
    opts = [correct] + parse_wrong(row["wrong_answers_json"])[:3]
    random.shuffle(opts)
    text = f"<b>🎯 {test['idx']+1}/{len(test['rows'])}</b>\n\n{row['question']}"
    if edit: await msg.edit_text(text, reply_markup=qkb(row["id"], opts, correct))
    else: await msg.answer(text, reply_markup=qkb(row["id"], opts, correct))

# qa_cb handles level test tracking too
@dp.callback_query(F.data.startswith("qa:"))
async def qa_cb(c: CallbackQuery):
    p = c.data.split(":")
    qid, idx = int(p[1]), int(p[2])
    row = q1("SELECT * FROM quiz_items WHERE id=?", (qid,))
    if not row: return await c.answer("❌", show_alert=True)
    correct = row["correct_answer"]
    opts = [correct] + parse_wrong(row["wrong_answers_json"])[:3]
    ok = opts[idx] == correct
    uid = c.from_user.id
    # Check if this is part of level test
    test = level_tests.get(uid)
    if test and test["idx"] < len(test["rows"]) and test["rows"][test["idx"]]["id"] == row["id"]:
        if ok: test["correct"] += 1
        test["idx"] += 1
        await c.message.edit_reply_markup(reply_markup=None)
        await lt_next(c.message, uid, True)
        await c.answer()
        return
    ux("INSERT INTO user_stats(user_id,total,correct,streak,best_streak,last_active) "
       "VALUES(?,1,?,?,?,date('now')) ON CONFLICT(user_id) DO UPDATE SET "
       "total=total+1,correct=correct+?,streak=CASE WHEN ? THEN streak+1 ELSE 0 END,"
       "best_streak=MAX(best_streak,CASE WHEN ? THEN streak+1 ELSE best_streak END),last_active=date('now')",
       (uid, 1 if ok else 0, 1 if ok else 0, 1 if ok else 0, ok, ok))
    ux("INSERT INTO seen_words(user_id,word_id,correct_count,wrong_count) VALUES(?,?,?,?) ON CONFLICT(user_id,word_id) "
       "DO UPDATE SET correct_count=correct_count+?,wrong_count=wrong_count+?,last_seen=datetime('now')",
       (uid, row["vocab_id"] if "vocab_id" in row else row["id"], 1 if ok else 0, 0 if ok else 1, 1 if ok else 0, 0 if ok else 1))
    # Track topic quiz results
    if "topic" in row:
        ux("INSERT INTO topic_mastery(user_id,topic,total_quiz,correct_quiz) VALUES(?,?,1,?) "
           "ON CONFLICT(user_id,topic) DO UPDATE SET total_quiz=total_quiz+1,correct_quiz=correct_quiz+?",
           (uid, row["topic"], 1 if ok else 0, 1 if ok else 0))
    if ok: add_xp(uid, 5)
    else: add_xp(uid, 1)
    add_activity(uid)
    # Check daily goal
    t = date.today().isoformat()
    ux("INSERT INTO daily_goals(user_id,date,goal,done) VALUES(?,?,10,1) ON CONFLICT(user_id,date) DO UPDATE SET done=done+1", (uid, t))
    goal, done = check_goal(uid)
    await c.message.edit_reply_markup(reply_markup=None)
    text = "✅ <b>To'g'ri!</b>" if ok else f"❌ <b>Noto'g'ri!</b>\n✅ <b>{correct}</b>"
    if done >= goal: text += "\n\n🎯 <b>Kunlik maqsad bajarildi!</b>"
    btns = []
    for o in opts:
        lbl = f"✅ {o}" if o == correct else (f"❌ {o}" if o == opts[idx] and not ok else o)
        btns.append([btn(lbl, "noop")])
    await c.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    new_badges = get_badges(uid)
    for b in new_badges: await c.message.answer(f"🎉 <b>Yangi yutuq!</b> {b}")
    await c.answer()

# ===== STATS =====
@dp.message(Command("stats"))
@dp.message(F.text == "📊 Statistika")
async def stats_(m: Message):
    try:
        uid = m.from_user.id
        s = uq1("SELECT * FROM user_stats WHERE user_id=?", (uid,))
        total = s["total"] if s else 0
        correct = s["correct"] if s else 0
        streak = s["streak"] if s else 0
        best_streak = s["best_streak"] if s else 0
        xp = s["xp"] if s else 0
        lvl = s["vocab_level"] if s else 1
        chat_count = s["chat_count"] if s else 0
        acc = (correct/total*100) if total > 0 else 0
        fav = uq1("SELECT COUNT(*) as c FROM user_favorites WHERE user_id=?", (uid,))
        badges = uq("SELECT badge FROM badges WHERE user_id=?", (uid,))
        seen = uq1("SELECT COUNT(*) as c FROM seen_words WHERE user_id=?", (uid,))["c"]
        act = uq1("SELECT COUNT(*) as c FROM user_activity WHERE user_id=?", (uid,))["c"]
        days = uq1("SELECT COUNT(DISTINCT date) as c FROM user_activity WHERE user_id=?", (uid,))["c"]
        notes = uq1("SELECT COUNT(*) as c FROM user_notes WHERE user_id=?", (uid,))["c"]
        reviews = uq1("SELECT COUNT(*) as c FROM seen_words WHERE user_id=? AND next_review<=date('now')", (uid,))["c"]
        pts = uq("SELECT topic,seen,total,correct_quiz,total_quiz FROM topic_mastery WHERE user_id=? ORDER BY total DESC LIMIT 5", (uid,))
        goal, done = check_goal(uid)
        pct_done = min(100, int(done/goal*100)) if goal > 0 else 0
        bar = "▓"*(pct_done//10) + "░"*(10-pct_done//10)
        await m.answer(
            f"<b>📊 Statistika</b>\n\n"
            f"<b>👤 Siz:</b>\n"
            f"  📝 {total} ta savol\n"
            f"  ✅ {correct} to'g'ri\n"
            f"  📈 {acc:.1f}% aniqlik\n"
            f"  🔥 {streak} kunlik seriya\n"
            f"  🏆 Eng yaxshi: {best_streak}\n"
            f"  📚 {seen} ta so'z ko'rgan\n"
            f"  ⭐ {fav['c'] if fav else 0} sevimli\n"
            f"  📝 {notes} ta note\n"
            f"  💬 {chat_count} ta chat\n"
            f"  🎮 {act} ta harakat\n"
            f"  📅 {days} kun faol\n"
            f"  ⚡ {xp} XP (Level {lvl})\n"
            f"  🏅 {len(badges)} ta yutuq\n"
            f"  🔄 {reviews} ta takrorlash\n\n"
            f"<b>🎯 Kunlik maqsad:</b> {done}/{goal}\n{bar} {pct_done}%\n\n"
            f"<b>📚 Baza:</b>\n  📝 {word_count()} so'z\n"
            f"  📂 {topic_count()} mavzu\n  ❓ {quiz_count()} test",
            reply_markup=mk())
    except Exception as e: await m.answer(f"❌ {e}")

# ===== TOPIC MASTERY =====
@dp.message(F.text == "📈 Mavzular")
async def topic_mastery(m: Message):
    try:
        uid = m.from_user.id
        mastery = uq("SELECT * FROM topic_mastery WHERE user_id=? ORDER BY total DESC", (uid,))
        if not mastery: return await m.answer("📈 Hali hech qanday mavzuni ko'rmagansiz.\n/topics dan boshlang.")
        lines = ["<b>📈 Mavzular bo'yicha progress</b>\n\n"]
        for r in mastery:
            seen_pct = min(100, int(r["seen"]/max(r["total"],1)*100))
            quiz_pct = min(100, int(r["correct_quiz"]/max(r["total_quiz"],1)*100)) if r["total_quiz"] > 0 else 0
            bar = "▓"*(seen_pct//10)+"░"*(10-seen_pct//10)
            lines.append(f"<b>{r['topic']}</b>")
            lines.append(f"  📚 {r['seen']}/{r['total']} so'z {bar}")
            if r["total_quiz"] > 0: lines.append(f"  ❓ {r['correct_quiz']}/{r['total_quiz']} test ({quiz_pct}%)")
            lines.append("")
        await m.answer("\n".join(lines), reply_markup=mk())
    except Exception as e: await m.answer(f"❌ {e}")

# ===== LEADERBOARD =====
@dp.message(Command("leaderboard"))
@dp.message(F.text == "🏅 Reyting")
async def lb(m: Message):
    try:
        rows = uq("SELECT user_id,total,correct,xp,streak FROM user_stats ORDER BY xp DESC LIMIT 10")
        if not rows: return await m.answer("🏅 Reyting bo'sh.")
        lines = ["<b>🏅 Reyting (Top 10)</b>\n"]
        for i, r in enumerate(rows, 1):
            medal = ["🥇","🥈","🥉",""][i-1] if i <= 3 else ""
            lines.append(f"{medal} <b>#{i}</b> | XP: {r['xp']} | Test: {r['total']} | Streak: {r['streak']}")
        await m.answer("\n".join(lines), reply_markup=mk())
    except Exception as e: await m.answer(f"❌ {e}")

# ===== BADGES =====
@dp.message(Command("badges"))
@dp.message(F.text == "🏆 Yutuqlar")
async def badges_(m: Message):
    try:
        earned = {r["badge"] for r in uq("SELECT badge FROM badges WHERE user_id=?", (m.from_user.id,))}
        all_b = [("📚 Yangi o'quvchi",5),("🎯 So'z ovchi",25),("⭐ Bilimdon",100),
                 ("🏆 Lug'at ustasi",500),("👑 So'zlar qiroli",1000),("💎 Ensiklopediya",3000),
                 ("📅 Haftalik faol","7 kun"),("🔥 50 kunlik seriya","50 kun")]
        lines = ["<b>🏆 Yutuqlar</b>\n"]
        for name, need in all_b:
            status = "✅" if name in earned else "❌"
            lines.append(f"{status} <b>{name}</b> — {need}")
        await m.answer("\n".join(lines), reply_markup=mk())
    except Exception as e: await m.answer(f"❌ {e}")

# ===== FAVORITES =====
@dp.message(Command("fav"))
@dp.message(F.text == "⭐ Sevimlilar")
async def fav_(m: Message):
    favs = uq("SELECT word_id FROM user_favorites WHERE user_id=? ORDER BY added_at DESC", (m.from_user.id,))
    if not favs: return await m.answer("⭐ Sevimlilar bo'sh.\nSo'z ustidagi ⭐ tugmasini bosing.")
    ids = [r["word_id"] for r in favs]
    try:
        for cs in range(0, len(ids), 10):
            chunk = ids[cs:cs+10]
            for i, r in enumerate(q(f"SELECT * FROM vocab_enriched WHERE id IN ({','.join('?'*len(chunk))})", chunk), cs+1):
                await m.answer(fmt_vocab(r, i), reply_markup=gkb(r["id"], True))
    except Exception as e: await m.answer(f"❌ {e}")

@dp.callback_query(F.data.startswith("fav:"))
async def fav_cb(c: CallbackQuery):
    wid = int(c.data.split(":")[1])
    ux("INSERT OR IGNORE INTO user_favorites(user_id,word_id) VALUES(?,?)", (c.from_user.id, wid))
    await c.answer("⭐ Qo'shildi!", show_alert=False)

@dp.callback_query(F.data.startswith("unfav:"))
async def unfav_cb(c: CallbackQuery):
    wid = int(c.data.split(":")[1])
    ux("DELETE FROM user_favorites WHERE user_id=? AND word_id=?", (c.from_user.id, wid))
    await c.answer("💔 O'chirildi!", show_alert=False)
    await c.message.edit_reply_markup(reply_markup=gkb(wid, False))

# ===== NOTES =====
@dp.callback_query(F.data.startswith("note:"))
async def note_cb(c: CallbackQuery):
    wid = int(c.data.split(":")[1])
    existing = uq1("SELECT note FROM user_notes WHERE user_id=? AND word_id=?", (c.from_user.id, wid))
    txt = existing["note"] if existing else ""
    await c.message.answer(
        f"📝 <b>Eslatma</b>\n\nHozirgi: {txt if txt else 'bo\'sh'}\n\n"
        f"Eslatma yozish uchun: /note {wid} <matn>\nO'chirish: /note_del {wid}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Orqaga", "noop")]]))
    await c.answer()

@dp.message(Command("note"))
async def note_cmd(m: Message):
    p = m.text.split(maxsplit=2)
    if len(p) < 3: return await m.answer("📝 /note <word_id> <eslatma>")
    try:
        wid, note = int(p[1]), p[2]
        ux("INSERT INTO user_notes(user_id,word_id,note) VALUES(?,?,?) ON CONFLICT(user_id,word_id) DO UPDATE SET note=?",
           (m.from_user.id, wid, note, note))
        await m.answer("✅ Eslatma saqlandi!")
    except: await m.answer("❌ Noto'g'ri ID.")

@dp.message(Command("note_del"))
async def note_del(m: Message):
    p = m.text.split(maxsplit=1)
    if len(p) < 2: return await m.answer("/note_del <word_id>")
    try:
        ux("DELETE FROM user_notes WHERE user_id=? AND word_id=?", (m.from_user.id, int(p[1])))
        await m.answer("✅ Eslatma o'chirildi.")
    except: await m.answer("❌ Xatolik.")

# ===== EXPORT =====
@dp.message(Command("export"))
@dp.message(F.text == "📥 Yuklash")
async def export_(m: Message):
    favs = uq("SELECT word_id FROM user_favorites WHERE user_id=? ORDER BY added_at DESC", (m.from_user.id,))
    if not favs: return await m.answer("⭐ Sevimlilar bo'sh.")
    ids = [r["word_id"] for r in favs]
    rows = q(f"SELECT * FROM vocab_enriched WHERE id IN ({','.join('?'*len(ids))})", ids)
    text = "English Vocabulary Master — Sevimlilar\n"+"="*40+"\n"
    for i, r in enumerate(rows, 1): text += f"\n{i}. {r['english']} — {r['uzbek']}\n   {r['definition']}\n"
    await m.answer_document(InputFile(io.BytesIO(text.encode()), filename="favorites.txt"), caption="⭐ Sevimlilar")

# ===== SRS REVIEW =====
@dp.message(Command("review"))
@dp.message(F.text == "🔄 Takrorlash")
async def review_(m: Message):
    uid = m.from_user.id
    try:
        rows = uq("SELECT word_id,srs_level FROM seen_words WHERE user_id=? AND (next_review IS NULL OR next_review<=date('now')) ORDER BY srs_level ASC, last_seen ASC LIMIT 8", (uid,))
        if not rows:
            rows = uq("SELECT word_id FROM seen_words WHERE user_id=? ORDER BY RANDOM() LIMIT 5", (uid,))
            if not rows: return await m.answer("🔄 Takrorlash uchun so'z yo'q.\nTest ishlang yoki so'z qidiring.")
        ids = [r["word_id"] for r in rows]
        words = q(f"SELECT * FROM vocab_enriched WHERE id IN ({','.join('?'*len(ids))})", ids)
        await m.answer(f"🔄 <b>Takrorlash</b> ({len(words)} ta so'z) — SRS tizimi:")
        for i, r in enumerate(words, 1):
            srs = uq1("SELECT srs_level FROM seen_words WHERE user_id=? AND word_id=?", (uid, r["id"]))
            lvl = srs["srs_level"] if srs else 0
            intervals = {0:"🔴 1 kun",1:"🟠 3 kun",2:"🟡 7 kun",3:"🟢 14 kun",4:"🔵 30 kun",5:"💎 60 kun"}
            lbl = intervals.get(lvl, f"✅ {2**lvl} kun")
            await m.answer(f"{fmt_vocab(r, i)}\nSRS: {lbl}",
                          reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                              [btn("✅ Esimda", f"srs_easy:{r['id']}"),
                               btn("🔄 Takror", f"srs_hard:{r['id']}"),
                               btn("❌ Unutdim", f"srs_again:{r['id']}")]]))
    except Exception as e: await m.answer(f"❌ {e}")

@dp.callback_query(F.data.startswith("srs_easy:"))
async def srs_easy(c: CallbackQuery):
    wid = int(c.data.split(":")[1]); uid = c.from_user.id
    srs = uq1("SELECT srs_level FROM seen_words WHERE user_id=? AND word_id=?", (uid, wid))
    lvl = min((srs["srs_level"] if srs else 0)+1, 8)
    ux("UPDATE seen_words SET srs_level=?,correct_count=correct_count+1,next_review=? WHERE user_id=? AND word_id=?", (lvl, srs_next_review(lvl), uid, wid))
    await c.answer(f"✅ Keyingi safar {srs_next_review(lvl)} da!", show_alert=False)
    await c.message.edit_reply_markup(reply_markup=None)

@dp.callback_query(F.data.startswith("srs_hard:"))
async def srs_hard(c: CallbackQuery):
    wid = int(c.data.split(":")[1]); uid = c.from_user.id
    srs = uq1("SELECT srs_level FROM seen_words WHERE user_id=? AND word_id=?", (uid, wid))
    lvl = max((srs["srs_level"] if srs else 0)-1, 0)
    ux("UPDATE seen_words SET srs_level=?,next_review=? WHERE user_id=? AND word_id=?", (lvl, srs_next_review(lvl), uid, wid))
    await c.answer(f"🔄 Keyingi safar {srs_next_review(lvl)} da", show_alert=False)
    await c.message.edit_reply_markup(reply_markup=None)

@dp.callback_query(F.data.startswith("srs_again:"))
async def srs_again(c: CallbackQuery):
    wid = int(c.data.split(":")[1]); uid = c.from_user.id
    ux("UPDATE seen_words SET srs_level=0,wrong_count=wrong_count+1,next_review=? WHERE user_id=? AND word_id=?", (date.today().isoformat(), uid, wid))
    await c.answer("🔴 Ertaga yana ko'ramiz!", show_alert=False)
    await c.message.edit_reply_markup(reply_markup=None)

# ===== WORD OF DAY =====
@dp.message(Command("word"))
@dp.message(F.text == "📖 Kundagi so'z")
async def wod(m: Message):
    try:
        row = get_wod()
        if not row: return await m.answer("❌ So'z topilmadi.")
        await m.answer(f"<b>📖 {date.today().isoformat()} — Kundagi so'z</b>\n\n{fmt_ext(row)}\n\n🤖 /gen {row['english']}", reply_markup=gkb(row["id"]))
    except Exception as e: await m.answer(f"❌ {e}")

# ===== ACTIVITY CALENDAR =====
@dp.message(Command("activity"))
@dp.message(F.text == "📅 Faoliyat")
async def activity_(m: Message):
    uid = m.from_user.id
    try:
        rows = uq("SELECT date, actions FROM user_activity WHERE user_id=? ORDER BY date DESC LIMIT 365", (uid,))
        if not rows: return await m.answer("📅 Hali hech qanday faoliyat yo'q.")
        act_map = {r["date"]: r["actions"] for r in rows}
        max_act = max(act_map.values()) if act_map else 1
        lines = ["<b>📅 Faoliyat kalendari</b>\n\n"]
        today = date.today()
        for day_offset in range(30, 0, -1):
            d = (today - timedelta(days=day_offset)).isoformat()
            act = act_map.get(d, 0)
            if act == 0: bar = "⬜"
            elif act < max_act * 0.33: bar = "🟩"
            elif act < max_act * 0.66: bar = "🟨"
            else: bar = "🟧"
            lines.append(bar)
            if day_offset % 7 == 1: lines.append(f" {d}\n")
        lines.append(f"\n⬜ 0   🟩 kam   🟨 o'rtacha   🟧 ko'p")
        await m.answer("".join(lines), reply_markup=mk())
    except Exception as e: await m.answer(f"❌ {e}")
@dp.message(Command("goal"))
async def goal_(m: Message):
    p = m.text.split(maxsplit=1)
    if len(p) >= 2:
        try:
            g = max(1, min(100, int(p[1].strip())))
            ux("UPDATE user_stats SET daily_goal=? WHERE user_id=?", (g, m.from_user.id))
            await m.answer(f"✅ Kunlik maqsad {g} ta test qilib belgilandi!")
        except: await m.answer("❌ /goal <son> (1-100)")
    else:
        goal, done = check_goal(m.from_user.id)
        await m.answer(f"🎯 <b>Kunlik maqsad</b>\n\nBajarildi: {done}/{goal}\n\n/goal <son> — o'zgartirish")

# ===== SETTINGS =====
@dp.message(Command("settings"))
@dp.message(F.text == "⚙️ Sozlamalar")
async def settings_(m: Message):
    uid = m.from_user.id
    s = uq1("SELECT * FROM user_settings WHERE user_id=?", (uid,))
    st = uq1("SELECT daily_goal FROM user_stats WHERE user_id=?", (uid,))
    goal = st["daily_goal"] if st else 10
    rem_time = s["reminder_time"] if s and "reminder_time" in s and s["reminder_time"] else "09:00"
    await m.answer(
        f"<b>⚙️ Sozlamalar</b>\n\n"
        f"🌐 Til: {s['language'] if s else 'uz'}\n"
        f"🎯 Kunlik maqsad: {goal} test\n"
        f"🔔 Eslatma: {'Yoqilgan' if s and 'daily_reminder' in s and s['daily_reminder'] else 'O\'chirilgan'} ({rem_time})\n\n"
        f"/goal <son> — maqsadni o'zgartirish\n"
        f"/reminder_on — eslatmani yoqish\n"
        f"/reminder_off — eslatmani o'chirish\n"
        f"/reminder_time <HH:MM> — eslatma vaqtini o'zgartirish",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn("🔔 Eslatma yoqish", "rem_on")]]))

@dp.callback_query(F.data == "rem_on")
async def rem_on(c: CallbackQuery):
    ux("UPDATE user_settings SET daily_reminder=1 WHERE user_id=?", (c.from_user.id,))
    await c.answer("✅ Eslatma yoqildi!", show_alert=False)

@dp.message(Command("reminder_on"))
async def reminder_on(m: Message):
    ux("UPDATE user_settings SET daily_reminder=1 WHERE user_id=?", (m.from_user.id,))
    await m.answer("✅ Kunlik eslatma yoqildi!")

@dp.message(Command("reminder_off"))
async def reminder_off(m: Message):
    ux("UPDATE user_settings SET daily_reminder=0 WHERE user_id=?", (m.from_user.id,))
    await m.answer("✅ Kunlik eslatma o'chirildi.")

@dp.message(Command("reminder_time"))
async def reminder_time(m: Message):
    p = m.text.split(maxsplit=1)
    if len(p) < 2: return await m.answer("⏰ /reminder_time <HH:MM>\nMasalan: /reminder_time 08:00")
    t = p[1].strip()
    try:
        datetime.strptime(t, "%H:%M")
        ux("UPDATE user_settings SET reminder_time=? WHERE user_id=?", (t, m.from_user.id))
        await m.answer(f"✅ Eslatma vaqti {t} ga o'zgartirildi!")
    except: await m.answer("❌ Noto'g'ri format. HH:MM (masalan: 09:00)")

# ===== GRAMMAR =====
@dp.message(Command("grammar"))
@dp.message(F.text == "📚 Grammar")
async def grammar(m: Message):
    try:
        sections = q("SELECT * FROM grammar_sections ORDER BY display_order")
        if not sections: return await m.answer("Grammar topilmadi.")
        lines = ["<b>📚 Grammar Sections</b>\n"]
        for s in sections:
            pc = q1("SELECT COUNT(*) FROM grammar_patterns WHERE section_code=?", (s["code"],))[0]
            lines.append(f"  • <b>{s['title_en']}</b> — {pc} patterns")
        lines.append("\n/grammar_browse — Barcha patterns\n/grammar_part1, /grammar_part2, /grammar_part3\n/grammar_quiz\n/check_grammar")
        await m.answer("\n".join(lines), reply_markup=mk())
    except Exception as e: await m.answer(f"❌ {e}")

@dp.message(Command("grammar_browse"))
async def gbrowse(m: Message):
    try:
        patterns = q("SELECT * FROM grammar_patterns ORDER BY category, title_en")
        if not patterns: return await m.answer("Patterns topilmadi.")
        cats = defaultdict(list)
        for p in patterns: cats[p["category"]].append(p)
        lines = ["<b>📚 Barcha Patterns</b>\n"]
        for cat, pats in cats.items():
            lines.append(f"\n<b>{cat}</b> ({len(pats)}):")
            for p in pats: lines.append(f"  • <code>{p['title_en']}</code> — {p['level']}")
        full = "\n".join(lines)
        for i in range(0, len(full), 4000): await m.answer(full[i:i+4000])
    except Exception as e: await m.answer(f"❌ {e}")

@dp.message(Command("grammar_part1"))
async def gp1(m: Message):
    try:
        p = q1("SELECT * FROM grammar_patterns WHERE ielts_part='Part 1' OR section_code='PART1_SHORT' ORDER BY RANDOM() LIMIT 1")
        if not p: return await m.answer("Part 1 topilmadi.")
        await m.answer(fmt_grammar(p, "IELTS Part 1 Grammar"))
    except Exception as e: await m.answer(f"❌ {e}")

@dp.message(Command("grammar_part2"))
async def gp2(m: Message):
    try:
        p = q1("SELECT * FROM grammar_patterns WHERE ielts_part='Part 2' OR section_code='PART2_STORY' ORDER BY RANDOM() LIMIT 1")
        if not p: return await m.answer("Part 2 topilmadi.")
        await m.answer(fmt_grammar(p, "IELTS Part 2 Story Grammar"))
    except Exception as e: await m.answer(f"❌ {e}")

@dp.message(Command("grammar_part3"))
async def gp3(m: Message):
    try:
        p = q1("SELECT * FROM grammar_patterns WHERE ielts_part='Part 3' OR section_code='PART3_DISCUSSION' ORDER BY RANDOM() LIMIT 1")
        if not p: return await m.answer("Part 3 topilmadi.")
        await m.answer(fmt_grammar(p, "IELTS Part 3 Discussion Grammar"))
    except Exception as e: await m.answer(f"❌ {e}")

@dp.message(Command("grammar_quiz"))
async def gq(m: Message):
    try:
        item = q1("SELECT qi.*, gp.title_en as pattern_title, gp.formula FROM grammar_quiz_items qi "
                  "JOIN grammar_patterns gp ON gp.id=qi.pattern_id ORDER BY RANDOM() LIMIT 1")
        if not item: return await m.answer("Grammar quiz topilmadi.")
        correct = item["correct_answer"]
        opts = [correct] + parse_wrong(item["wrong_answers_json"])[:3]
        random.shuffle(opts)
        formula = item['formula'] if 'formula' in item and item['formula'] else 'N/A'
        question = item['question'] if 'question' in item and item['question'] else 'N/A'
        await m.answer(
            f"<b>📝 Grammar Quiz</b>\n\nPattern: {_g(item,'pattern_title','N/A')}\n"
            f"Formula: <code>{formula}</code>\n\n<b>Question:</b>\n{question}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn(o[:40], f"gq:{item['id']}:{i}")] for i,o in enumerate(opts)]))
    except Exception as e: await m.answer(f"❌ {e}")

@dp.callback_query(F.data.startswith("gq:"))
async def gq_cb(c: CallbackQuery):
    p = c.data.split(":")
    qid, idx = int(p[1]), int(p[2])
    item = q1("SELECT * FROM grammar_quiz_items WHERE id=?", (qid,))
    if not item: return await c.answer("❌", show_alert=True)
    correct = item["correct_answer"]
    opts = [correct] + parse_wrong(item["wrong_answers_json"])[:3]
    ok = opts[idx] == correct
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer("✅ <b>Correct!</b>" if ok else f"❌ <b>Wrong!</b>\n✅ <b>{correct}</b>")
    await c.answer()

@dp.message(Command("check_grammar"))
async def check_(m: Message):
    p = m.text.split(maxsplit=1)
    if len(p) < 2: return await m.answer("📝 /check_grammar <sentence>\nMasalan: I go to school yesterday")
    sent = p[1].strip()
    if not OPENROUTER_API_KEY: return await m.answer("⚠️ OpenRouter kaliti yo'q.")
    msg = await m.answer("⏳ Checking...")
    try:
        r = await gen_ai(f"""IELTS English teacher. Analyze: "{sent}". Return valid JSON:
{{"is_correct":true/false,"corrected_answer":"...","better_version":"...","explanation_uz":"...","score":8,"grammar_issues":["..."]}}""", 0.3)
        if not r: return await msg.edit_text("❌ API xatosi.")
        if r.startswith("```"): r = r.split("\n",1)[1] if "\n" in r else r[3:]
        if r.endswith("```"): r = r.rsplit("```",1)[0]
        res = json.loads(r.strip())
        ok = res.get("is_correct", False)
        reply = f"{'✅' if ok else '❌'} <b>Grammar Check</b>\n\n<b>You:</b> {sent}\n"
        reply += f"<b>Status:</b> {'Correct' if ok else 'Needs work'}\n"
        reply += f"<b>Score:</b> {res.get('score',0)}/10\n\n"
        if res.get("corrected_answer"): reply += f"<b>Corrected:</b> {res['corrected_answer']}\n"
        if res.get("better_version"): reply += f"<b>Better:</b> {res['better_version']}\n"
        if res.get("grammar_issues"): reply += f"<b>Issues:</b> {', '.join(res['grammar_issues'])}\n"
        if res.get("explanation_uz"): reply += f"\n<b>💡</b> {res['explanation_uz']}"
        await msg.edit_text(reply); add_xp(m.from_user.id, 3)
    except Exception as e: await msg.edit_text(f"❌ {e}")

# ===== CALLBACKS =====
@dp.callback_query(F.data.startswith("gen:"))
async def gen_cb(c: CallbackQuery):
    wid = int(c.data.split(":")[1])
    row = q1("SELECT * FROM vocab_enriched WHERE id=?", (wid,))
    if not row: return await c.answer("❌", show_alert=True)
    await c.answer(); await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer(f"🤖 <b>{row['english']}</b> — rejim:", reply_markup=mkb(wid))

@dp.callback_query(F.data.startswith("mode:"))
async def mode_cb(c: CallbackQuery):
    p = c.data.split(":"); wid, mode = int(p[1]), p[2]
    row = q1("SELECT * FROM vocab_enriched WHERE id=?", (wid,))
    if not row: return await c.answer("❌", show_alert=True)
    await c.answer("⏳ AI..."); await c.message.edit_reply_markup(reply_markup=None)
    r = await gen_ai(f"""Create ONE sentence using "{row['english']}" ({row['uzbek']}). Mode: {mode}. Topic: {row['topic']}, Level: {row['level']}. Return JSON: {{"sentence_en":"...","sentence_uz":"...","explanation_uz":"..."}}""")
    if not r: return await c.message.answer(f"❌ AI xatosi.\n/gen {row['english']}")
    if r.startswith("```"): r = r.split("\n",1)[1] if "\n" in r else r[3:]
    if r.endswith("```"): r = r.rsplit("```",1)[0]
    try:
        res = json.loads(r.strip())
        await c.message.answer(f"<b>🤖 AI ({mode})</b>\n\n<b>🇬🇧</b> {res.get('sentence_en','')}\n<b>🇺🇿</b> {res.get('sentence_uz','')}\n\n<b>💡</b> {res.get('explanation_uz','')}")
    except: await c.message.answer(r)

@dp.callback_query(F.data == "noop")
async def noop(c: CallbackQuery): await c.answer()

# ===== INLINE =====
@dp.inline_query()
async def inline_query(q: types.InlineQuery):
    text = q.query.strip()
    if len(text) < 2: return await q.answer([InlineQueryResultArticle(id="help",title="So'zni kiriting...",
        input_message_content=InputTextMessageContent("🔍 English yoki Uzbek tilida so'z yozing"),
        description="2 ta belgidan ko'p bo'lishi kerak")])
    pat = f"%{text}%"
    try:
        rows = q("SELECT * FROM vocab_enriched WHERE english LIKE ? OR uzbek LIKE ? LIMIT 15", (pat, pat))
        results = []
        for r in rows:
            results.append(InlineQueryResultArticle(id=str(r["id"]),
                title=f"{r['english']} — {r['uzbek']}",
                description=f"{r['topic']} | {r['level']} | {_g(r,'definition')[:80]}",
                input_message_content=InputTextMessageContent(fmt_vocab(r)),
                reply_markup=gkb(r["id"])))
        await q.answer(results[:20], cache_time=60, is_personal=True)
    except: await q.answer([], cache_time=60, is_personal=True)

# ===== ADMIN =====
import time as _time
@dp.message(Command("admin"))
async def admin(m: Message):
    if m.from_user.id not in ADMIN_IDS: return await m.answer("❌ Ruxsat yo'q.")
    users = uq1("SELECT COUNT(*) as c FROM user_stats")["c"]
    favs = uq1("SELECT COUNT(*) as c FROM user_favorites")["c"]
    act = uq1("SELECT COUNT(*) as c FROM user_activity")["c"]
    seen = uq1("SELECT COUNT(*) as c FROM seen_words")["c"]
    notes = uq1("SELECT COUNT(*) as c FROM user_notes")["c"]
    writing = uq1("SELECT COUNT(*) as c FROM writing_log")["c"]
    today_act = uq1("SELECT COUNT(*) as c FROM user_activity WHERE date=?", (date.today().isoformat(),))["c"]
    uptime = int(_time.time())
    await m.answer(f"<b>👑 Admin Panel</b>\n\n"
        f"Users: {users}\nActive today: {today_act}\n"
        f"Favorites: {favs}\nSeen words: {seen}\n"
        f"Notes: {notes}\nWriting: {writing}\n"
        f"Activities: {act}\n"
        f"DB: {DB_PATH.stat().st_size/1024/1024:.1f} MB\n"
        f"User DB: {Path(__file__).resolve().parent.joinpath('user_data.db').stat().st_size/1024:.1f} KB\n\n"
        f"/broadcast <matn> — hammaga xabar yuborish\n"
        f"/user_stats <id> — foydalanuvchi statistikasi")

@dp.message(Command("broadcast"))
async def broadcast(m: Message):
    if m.from_user.id not in ADMIN_IDS: return await m.answer("❌ Ruxsat yo'q.")
    p = m.text.split(maxsplit=1)
    if len(p) < 2: return await m.answer("📢 /broadcast <matn>\nBarcha foydalanuvchilarga xabar yuborish.")
    text = p[1].strip()
    users = uq("SELECT user_id FROM user_stats")
    sent, failed = 0, 0
    msg = await m.answer(f"📢 Xabar yuborilmoqda: {len(users)} ta foydalanuvchiga...")
    for u in users:
        try:
            await bot.send_message(u["user_id"], f"📢 <b>Admin xabari</b>\n\n{text}")
            sent += 1
        except:
            failed += 1
        await asyncio.sleep(0.05)
    await msg.edit_text(f"✅ Yuborildi: {sent} ta\n❌ Yetmadi: {failed} ta")

@dp.message(Command("user_stats"))
async def admin_user_stats(m: Message):
    if m.from_user.id not in ADMIN_IDS: return await m.answer("❌ Ruxsat yo'q.")
    p = m.text.split(maxsplit=1)
    if len(p) < 2: return await m.answer("/user_stats <user_id>")
    try:
        uid = int(p[1].strip())
        s = uq1("SELECT * FROM user_stats WHERE user_id=?", (uid,))
        if not s: return await m.answer(f"❌ {uid} topilmadi.")
        fav = uq1("SELECT COUNT(*) as c FROM user_favorites WHERE user_id=?", (uid,))["c"]
        badges = uq("SELECT badge FROM badges WHERE user_id=?", (uid,))
        seen = uq1("SELECT COUNT(*) as c FROM seen_words WHERE user_id=?", (uid,))["c"]
        act = uq1("SELECT COUNT(*) as c FROM user_activity WHERE user_id=?", (uid,))["c"]
        days = uq1("SELECT COUNT(DISTINCT date) as c FROM user_activity WHERE user_id=?", (uid,))["c"]
        await m.answer(
            f"<b>👤 Foydalanuvchi: {uid}</b>\n\n"
            f"📝 Testlar: {s['total']}\n✅ To'g'ri: {s['correct']}\n"
            f"🔥 Streak: {s['streak']} | Eng yaxshi: {s['best_streak']}\n"
            f"⚡ XP: {s['xp']} | Level: {s['vocab_level']}\n"
            f"📚 Ko'rgan: {seen} | ⭐ Sevimli: {fav}\n"
            f"🎮 Harakat: {act} | 📅 Kun: {days}\n"
            f"🏅 Yutuqlar: {len(badges)}\n"
            f"💬 Chat: {s['chat_count'] if 'chat_count' in s else 0}\n"
            f"📝 Level test: {s['level_test_taken'] if 'level_test_taken' in s else 0} ball: {s['level_test_score'] if 'level_test_score' in s else 0}")
    except ValueError:
        await m.answer("❌ Noto'g'ri ID.")

# ===== FALLBACK =====
@dp.message()
async def fallback(m: Message):
    txt = m.text.strip()
    uid = m.from_user.id
    if uid in chat_sessions:
        chat_sessions[uid].append({"role":"user","content":txt})
        prompt = str(chat_sessions[uid][-8:])
        r = await gen_ai(f"""English tutor. Previous: {prompt}. Respond naturally, correct mistakes, explain in Uzbek if needed. Max 200 words.""")
        if r:
            chat_sessions[uid].append({"role":"assistant","content":r})
            ux("UPDATE user_stats SET chat_count=chat_count+1 WHERE user_id=?", (uid,)); add_xp(uid, 1)
            await m.answer(r)
        else: await m.answer("❌ AI xatosi. /chat_end")
        return
    # Writing mode detection (text > 3 sentences)
    if len(txt.split(".")) >= 3 and len(txt) > 50 and OPENROUTER_API_KEY:
        await m.answer("✍️ <b>Writing tekshirilmoqda...</b>")
        r = await gen_ai(f"""You are an IELTS writing examiner. Evaluate this text and return valid JSON:
Text: "{txt}"
{{"score":8,"feedback_uz":"...","mistakes":["..."],"correction":"...","grammar_score":8,"vocabulary_score":8,"suggestions_uz":"..."}}""", 0.3)
        if r:
            if r.startswith("```"): r = r.split("\n",1)[1] if "\n" in r else r[3:]
            if r.endswith("```"): r = r.rsplit("```",1)[0]
            try:
                res = json.loads(r.strip())
                ux("INSERT INTO writing_log(user_id,text,feedback,score) VALUES(?,?,?,?)", (uid, txt, res.get("feedback_uz",""), res.get("score",0)))
                reply = (f"✍️ <b>Writing Feedback</b>\n\n<b>Score:</b> {res.get('score',0)}/10\n"
                         f"<b>Grammar:</b> {res.get('grammar_score',0)}/10 | "
                         f"<b>Vocabulary:</b> {res.get('vocabulary_score',0)}/10\n\n")
                if res.get("feedback_uz"): reply += f"<b>💡</b> {res['feedback_uz']}\n"
                if res.get("correction"): reply += f"<b>Correction:</b> {res['correction']}\n"
                if res.get("suggestions_uz"): reply += f"\n<b>📝</b> {res['suggestions_uz']}"
                await m.answer(reply); add_xp(uid, 5)
                return
            except: pass
        await m.answer("✍️ Writing tekshirishda xatolik. /writing bilan qayta urinib ko'ring.")
        return
    # Word lookup
    try:
        pat = f"%{txt}%"
        rows = q("SELECT * FROM vocab_enriched WHERE english LIKE ? OR uzbek LIKE ? OR definition LIKE ? "
                 "ORDER BY CASE WHEN english LIKE ? THEN 0 WHEN uzbek LIKE ? THEN 1 ELSE 2 END LIMIT 4", (pat, pat, pat, txt, txt))
        if rows:
            await m.answer(f"🔍 <b>{txt}</b> — {len(rows)} ta:")
            for i, r in enumerate(rows, 1):
                ux("INSERT INTO seen_words(user_id,word_id,seen_count) VALUES(?,?,1) ON CONFLICT(user_id,word_id) DO UPDATE SET seen_count=seen_count+1,last_seen=datetime('now')", (uid, r["id"]))
                fav = uq1("SELECT 1 FROM user_favorites WHERE user_id=? AND word_id=?", (uid, r["id"]))
                await m.answer(fmt_vocab(r, i), reply_markup=gkb(r["id"], fav))
            add_activity(uid)
        else:
            r = await gen_ai(f"""User sent: "{txt}". If English-related, answer helpfully in Uzbek. Otherwise suggest /help. Short.""", 0.5, 300)
            if r: await m.answer(r)
            else: await m.answer("🤔 Noto'g'ri buyruq.\n/help", reply_markup=mk())
    except Exception as e: await m.answer(f"❌ {e}")

async def run_scheduled_reminders():
    while True:
        try:
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            current_time = now.strftime("%H:%M")
            users = uq("SELECT user_id, reminder_time, last_reminder_date FROM user_settings WHERE daily_reminder=1")
            for u in users:
                if u["last_reminder_date"] == today: continue
                if u["reminder_time"] and u["reminder_time"] <= current_time:
                    goal, done = check_goal(u["user_id"])
                    if done < goal:
                        try:
                            await bot.send_message(u["user_id"],
                                f"🔔 <b>Kunlik eslatma!</b>\n\n"
                                f"Bugungi maqsadingiz: {goal} ta test\n"
                                f"Bajarilgan: {done} ta\n\n"
                                f"🏃 Davom eting! /quiz")
                            ux("UPDATE user_settings SET last_reminder_date=? WHERE user_id=?", (today, u["user_id"]))
                        except: pass
            await asyncio.sleep(60)
        except Exception as e:
            logger.error(f"Reminder: {e}")
            await asyncio.sleep(60)

async def main():
    logger.info("🤖 Bot is starting...")
    asyncio.create_task(run_scheduled_reminders())
    await dp.start_polling(bot, handle_signals=False)

if __name__ == "__main__":
    asyncio.run(main())
