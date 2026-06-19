import os, json, random, sqlite3, asyncio, threading, logging, io, textwrap
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
    Message, CallbackQuery, InputFile,
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

DB_LOCK = threading.Lock()
CONN = None
USER_DB = threading.Lock()
U_CONN = None

def db():
    global CONN
    if CONN is None:
        CONN = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        CONN.row_factory = sqlite3.Row
        CONN.execute("PRAGMA journal_mode=WAL")
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
            vocab_level INTEGER DEFAULT 1, xp INTEGER DEFAULT 0, chat_count INTEGER DEFAULT 0)""")
        U_CONN.execute("""CREATE TABLE IF NOT EXISTS user_favorites (
            user_id INTEGER, word_id INTEGER, added_at TEXT DEFAULT (datetime('now')), PRIMARY KEY (user_id, word_id))""")
        U_CONN.execute("""CREATE TABLE IF NOT EXISTS word_of_day (date TEXT PRIMARY KEY, word_id INTEGER)""")
        U_CONN.execute("""CREATE TABLE IF NOT EXISTS seen_words (
            user_id INTEGER, word_id INTEGER, seen_count INTEGER DEFAULT 1,
            correct_count INTEGER DEFAULT 0, wrong_count INTEGER DEFAULT 0,
            last_seen TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, word_id))""")
        U_CONN.execute("""CREATE TABLE IF NOT EXISTS badges (
            user_id INTEGER, badge TEXT, earned_at TEXT DEFAULT (datetime('now')), PRIMARY KEY (user_id, badge))""")
        U_CONN.execute("""CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY, daily_reminder INTEGER DEFAULT 0, language TEXT DEFAULT 'uz')""")
        U_CONN.execute("""CREATE TABLE IF NOT EXISTS user_activity (
            user_id INTEGER, date TEXT, actions INTEGER DEFAULT 1, PRIMARY KEY (user_id, date))""")
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

# Cache
CACHE, CACHE_TTL = {}, 300
def cached(ttl=300):
    def deco(fn):
        def wrap(*a,**kw):
            k = fn.__name__
            n = datetime.now().timestamp()
            if k in CACHE and n - CACHE[k]["ts"] < ttl: return CACHE[k]["val"]
            v = fn(*a,**kw)
            CACHE[k] = {"val":v,"ts":n}
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

def get_wod():
    t = date.today().isoformat()
    r = uq1("SELECT word_id FROM word_of_day WHERE date=?", (t,))
    if r:
        row = q1("SELECT * FROM vocab_enriched WHERE id=?", (r["word_id"],))
        if row: return row
    row = q1("SELECT * FROM vocab_enriched ORDER BY RANDOM() LIMIT 1")
    if row: ux("INSERT OR REPLACE INTO word_of_day(date,word_id) VALUES(?,?)", (t, row["id"]))
    return row

def add_xp(uid, amount):
    ux("UPDATE user_stats SET xp=xp+? WHERE user_id=?", (amount, uid))
def add_activity(uid):
    t = date.today().isoformat()
    ux("INSERT INTO user_activity(user_id,date) VALUES(?,?) ON CONFLICT(user_id,date) DO UPDATE SET actions=actions+1", (uid, t))

def get_badges(uid):
    all_badges = [
        (5, "📚 Yangi o'quvchi", 5),
        (25, "🎯 So'z ovchi", 25),
        (100, "⭐ Bilimdon", 100),
        (500, "🏆 Lug'at ustasi", 500),
        (1000, "👑 So'zlar qiroli", 1000),
        (3000, "💎 Ensiklopediya", 3000),
    ]
    stats = uq1("SELECT * FROM user_stats WHERE user_id=?", (uid,))
    earned = {r["badge"] for r in uq("SELECT badge FROM badges WHERE user_id=?", (uid,))}
    new_badges = []
    if stats:
        total = stats["total"]
        for bname, _, need in all_badges:
            if total >= need and bname not in earned:
                ux("INSERT INTO badges(user_id,badge) VALUES(?,?)", (uid, bname))
                new_badges.append(bname)
    return new_badges

async def gen_ai(prompt, temp=0.7, max_t=800):
    if not OPENROUTER_API_KEY: return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as cl:
            r = await cl.post(f"{OPENROUTER_BASE_URL}/chat/completions",
                headers={"Authorization":f"Bearer {OPENROUTER_API_KEY}","Content-Type":"application/json"},
                json={"model":OPENROUTER_MODEL,"messages":[{"role":"user","content":prompt}],
                      "temperature":temp,"max_tokens":max_t})
        if r.status_code != 200: return None
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"AI error: {e}")
        return None

def fmt_vocab(row, idx=None):
    p = f"<b>{idx}.</b> " if idx else ""
    return (f"{p}<b>{row['english']}</b>  /{row['english']}\n"
            f"🇺🇿 {row['uzbek']}\n📂 <code>{row['topic']}</code> | "
            f"🏷 <code>{row['type']}</code> | 📊 {row['level']}\n"
            f"📖 {row.get('definition','')}\n📝 <i>{row.get('example_en','')}</i>\n🇺🇿 {row.get('example_uz','')}")

def fmt_extended(row):
    lines = [f"<b>📖 {row['english']}</b>"]
    lines.append(f"🇺🇿 <b>{row['uzbek']}</b>")
    lines.append(f"\n📂 Topic: {row['topic']}")
    lines.append(f"🏷 Type: {row['type']}")
    lines.append(f"📊 Level: {row['level']}")
    if row.get('phonetic'): lines.append(f"🔊 {row['phonetic']}")
    lines.append(f"\n📖 {row.get('definition','')}")
    lines.append(f"\n<b>Example:</b>")
    lines.append(f"🇬🇧 {row.get('example_en','')}")
    lines.append(f"🇺🇿 {row.get('example_uz','')}")
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
    lines.append(f"<b>{p['meaning_uz']}</b>\n{p['explanation_uz'][:400]}")
    if "when_to_use_uz" in p and p["when_to_use_uz"]:
        lines.append(f"\n<b>When to use:</b>\n{p['when_to_use_uz'][:300]}")
    ex = q("SELECT example_en,example_uz FROM grammar_examples WHERE pattern_id=? LIMIT 2", (p["id"],))
    if ex:
        lines.append("\n<b>Examples:</b>")
        for e in ex: lines.append(f"EN: {e['example_en']}\nUZ: {e['example_uz']}\n")
    return "\n".join(lines)

# ===================== KEYBOARDS =====================
def mk():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📋 Mavzular"), KeyboardButton(text="🏷 Turlar")],
        [KeyboardButton(text="🔍 Qidirish"), KeyboardButton(text="❓ Test"), KeyboardButton(text="📝 Maxsus test")],
        [KeyboardButton(text="🤖 AI Jumla"), KeyboardButton(text="💬 AI Chat"), KeyboardButton(text="📊 Statistika")],
        [KeyboardButton(text="⭐ Sevimlilar"), KeyboardButton(text="📖 Kundagi so'z"), KeyboardButton(text="🔄 Takrorlash")],
        [KeyboardButton(text="🏆 Yutuqlar"), KeyboardButton(text="🏅 Reyting"), KeyboardButton(text="📚 Grammar")],
        [KeyboardButton(text="ℹ️ Yordam")],
    ], resize_keyboard=True)

def btn(t, d): return InlineKeyboardButton(text=t, callback_data=d)

def paginate(items, page, pp, prefix):
    s, e = page*pp, page*pp+pp
    chunk = items[s:e]
    btns = []
    for i, item in enumerate(chunk):
        label = item.get('topic', item.get('type', item.get('english', str(item))))
        if 'count' in item: label = f"{label} ({item['count']})"
        btns.append([btn(label, f"{prefix}:{s+i}")])
    nav = []
    if page > 0: nav.append(btn("⬅️", f"{prefix}_pg:{page-1}"))
    if e < len(items): nav.append(btn("➡️", f"{prefix}_pg:{page+1}"))
    if nav: btns.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=btns)

def gkb(wid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("🤖 AI Jumla", f"gen:{wid}"), btn("⭐", f"fav:{wid}"), btn("🔊", f"tts:{wid}")]
    ])

def mkb(wid):
    modes = [("☀️ Daily","daily"),("🎤 Speaking","speaking"),("✍️ Writing","writing"),
             ("📄 Essay","essay"),("💼 Formal","formal")]
    return InlineKeyboardMarkup(inline_keyboard=[[btn(l,f"mode:{wid}:{m}")] for l,m in modes])

def qkb(qid, opts, correct):
    return InlineKeyboardMarkup(inline_keyboard=[[btn(o, f"qa:{qid}:{i}:{correct}")] for i,o in enumerate(opts)])

# ===================== HANDLERS =====================
@dp.message(CommandStart())
async def start(m: Message):
    uid = m.from_user.id
    ux("INSERT OR IGNORE INTO user_stats(user_id) VALUES(?)", (uid,))
    ux("INSERT OR IGNORE INTO user_settings(user_id) VALUES(?)", (uid,))
    await m.answer(
        "<b>📚 English Vocabulary Master</b>\n\n"
        "3480+ so'z, 24 mavzu, 32 grammar pattern\n"
        "AI jumla yaratish, grammatikani tekshirish\n\n"
        "👇 Tugmalardan foydalaning:", reply_markup=mk())

@dp.message(Command("help"))
@dp.message(F.text == "ℹ️ Yordam")
async def help_(m: Message):
    await m.answer(
        "<b>📋 Buyruqlar:</b>\n"
        "📋 /topics — Mavzular\n🏷 /types — Turlar\n"
        "🔍 /search &lt;so'z&gt; — Qidirish\n📂 /topic &lt;MAVZU&gt; — Mavzu bo'yicha\n"
        "❓ /quiz — Tasodifiy test\n📝 /custom_quiz — Maxsus test\n"
        "🤖 /gen &lt;so'z&gt; — AI jumla\n💬 /chat — AI bilan suhbat\n"
        "📊 /stats — Statistika\n🏅 /leaderboard — Reyting\n"
        "🏆 /badges — Yutuqlar\n⭐ /fav — Sevimlilar\n"
        "🔄 /review — Takrorlash\n📖 /word — Kundagi so'z\n"
        "📚 /grammar — Grammar\n/grammar_quiz — Grammar test\n"
        "/check_grammar + gap — AI grammar check\n"
        "/export — Sevimlilarni yuklab olish", reply_markup=mk())

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

@dp.callback_query(F.data.startswith("tp:"))
async def tp_cb(c: CallbackQuery):
    idx = int(c.data.split(":")[1])
    topics = get_topics()
    if idx >= len(topics): return await c.answer("❌", show_alert=True)
    topic = topics[idx]["topic"]
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=None)
    total = q1("SELECT COUNT(*) FROM vocab_enriched WHERE topic=?", (topic,))[0]
    rows = q("SELECT * FROM vocab_enriched WHERE topic=? ORDER BY RANDOM() LIMIT 10", (topic,))
    if not rows: return await c.message.answer(f"❌ <code>{topic}</code> bo'sh.")
    await c.message.answer(f"📂 <b>{topic}</b> — {total} so'z:")
    for i, r in enumerate(rows, 1): await c.message.answer(fmt_vocab(r, i), reply_markup=gkb(r["id"]))

@dp.message(Command("topic"))
async def topic_cmd(m: Message):
    p = m.text.split(maxsplit=1)
    if len(p) < 2: return await m.answer("📂 /topic <MAVZU>\nMasalan: /topic Education")
    t = p[1].strip().upper()
    try:
        rows = q("SELECT * FROM vocab_enriched WHERE topic=? ORDER BY RANDOM() LIMIT 10", (t,))
        if not rows: return await m.answer(f"❌ <code>{t}</code> bo'yicha so'z yo'q.")
        await m.answer(f"📂 <b>{t}</b> — {len(rows)} ta:")
        for i, r in enumerate(rows, 1): await m.answer(fmt_vocab(r, i), reply_markup=gkb(r["id"]))
    except Exception as e: await m.answer(f"❌ {e}")

# ===== TYPES =====
@dp.message(Command("types"))
@dp.message(F.text == "🏷 Turlar")
async def types_(m: Message):
    try:
        rows = type_dist()
        await m.answer("<b>🏷 So'z turlari:</b>\n" + "\n".join(f"  • <code>{r['type']}</code> — {r['count']} ta" for r in rows), reply_markup=mk())
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
                 "ORDER BY CASE WHEN english LIKE ? THEN 0 WHEN uzbek LIKE ? THEN 1 ELSE 2 END LIMIT 8",
                 (pat, pat, pat, q, q))
        if not rows: return await m.answer(f"❌ '{q}' bo'yicha topilmadi.")
        await m.answer(f"🔍 <b>{q}</b> — {len(rows)} ta:")
        for i, r in enumerate(rows, 1):
            fav = uq1("SELECT 1 FROM user_favorites WHERE user_id=? AND word_id=?", (m.from_user.id, r["id"]))
            await m.answer(("⭐ " if fav else "") + fmt_vocab(r, i), reply_markup=gkb(r["id"]))
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
        r = await gen_ai(f"""You are an English teacher for Uzbek students. Create ONE natural sentence using "{row['english']}".
Meanings: {row['uzbek']}. Topic: {row['topic']}, Level: {row['level']}
Return valid JSON: {{"sentence_en":"...","sentence_uz":"...","explanation_uz":"...","word":"{row['english']}"}}""")
        if not r: return await msg.edit_text("❌ AI xatosi.")
        txt = r
        if txt.startswith("```"): txt = txt.split("\n",1)[1] if "\n" in txt else txt[3:]
        if txt.endswith("```"): txt = txt.rsplit("```",1)[0]
        res = json.loads(txt.strip())
        await msg.edit_text(
            f"<b>🤖 AI Generated</b>\n\n<b>🇬🇧</b> {res.get('sentence_en','')}\n"
            f"<b>🇺🇿</b> {res.get('sentence_uz','')}\n\n<b>💡</b> {res.get('explanation_uz','')}")
        add_xp(m.from_user.id, 2)
    except Exception as e: await m.answer(f"❌ {e}")

# ===== AI CHAT =====
chat_sessions = {}
@dp.message(Command("chat"))
@dp.message(F.text == "💬 AI Chat")
async def chat_(m: Message):
    uid = m.from_user.id
    chat_sessions[uid] = [{"role":"system","content":"You are an English tutor for Uzbek students. "
        "Help them practice English. Be encouraging, correct mistakes gently. "
        "Explain in Uzbek when needed. Keep responses under 200 words."}]
    await m.answer("💬 <b>AI Chat</b> — Ingliz tilida suhbatlashing!\n"
        "Inglizcha yozing, men xatolarni tuzataman va tushuntiraman.\n/chat_end — tugatish", reply_markup=mk())

@dp.message(Command("chat_end"))
async def chat_end(m: Message):
    chat_sessions.pop(m.from_user.id, None)
    await m.answer("✅ Suhbat tugadi. /chat bilan qayta boshlang.")

@dp.message(Command("chat"))
@dp.message(F.text == "💬 AI Chat")
async def chat_start(m: Message):
    uid = m.from_user.id
    chat_sessions[uid] = [{"role":"system","content":"You are an English tutor for Uzbek students. "
        "Help them practice English. Be encouraging, correct mistakes gently. "
        "Explain in Uzbek when needed. Keep responses under 200 words."}]
    await m.answer("💬 <b>AI Chat</b> — Ingliz tilida suhbatlashing!\n"
        "Inglizcha yozing, men xatolarni tuzataman va tushuntiraman.\n/chat_end — tugatish", reply_markup=mk())

# ===== QUIZ =====
@dp.message(Command("quiz"))
@dp.message(F.text == "❓ Test")
async def quiz_(m: Message):
    p = m.text.split(maxsplit=1)
    q = p[1].strip().upper() if len(p) > 1 and not p[1].startswith("/") else None
    try:
        row = q1("SELECT * FROM quiz_items ORDER BY RANDOM() LIMIT 1") if not q else \
              q1("SELECT * FROM quiz_items WHERE topic=? ORDER BY RANDOM() LIMIT 1", (q,))
        if not row: return await m.answer(f"❌ Test topilmadi.")
        correct = row["correct_answer"]
        opts = [correct] + parse_wrong(row["wrong_answers_json"])[:3]
        random.shuffle(opts)
        await m.answer(f"<b>❓ Savol:</b>\n{row['question']}", reply_markup=qkb(row["id"], opts, correct))
    except Exception as e: await m.answer(f"❌ {e}")

# ===== CUSTOM QUIZ =====
@dp.message(Command("custom_quiz"))
@dp.message(F.text == "📝 Maxsus test")
async def cq_start(m: Message):
    topics = get_topics()
    await m.answer("📝 <b>Maxsus test</b>\nMavzuni tanlang:", reply_markup=paginate(topics, 0, 10, "cq"))

@dp.callback_query(F.data.startswith("cq_pg:"))
async def cq_page(c: CallbackQuery):
    page = int(c.data.split(":")[1])
    await c.message.edit_reply_markup(reply_markup=paginate(get_topics(), page, 10, "cq"))
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

@dp.callback_query(F.data.startswith("qa:"))
async def qa_cb(c: CallbackQuery):
    p = c.data.split(":")
    idx = int(p[2]); correct = p[3]
    row = q1("SELECT * FROM quiz_items WHERE id=?", (p[1],))
    if not row: return await c.answer("❌", show_alert=True)
    opts = [correct] + parse_wrong(row["wrong_answers_json"])[:3]
    random.shuffle(opts)
    ok = opts[idx] == correct
    uid = c.from_user.id
    ux("INSERT INTO user_stats(user_id,total,correct,streak,best_streak,last_active) "
       "VALUES(?,1,?,?,?,date('now')) ON CONFLICT(user_id) DO UPDATE SET "
       "total=total+1,correct=correct+?,streak=CASE WHEN ? THEN streak+1 ELSE 0 END,"
       "best_streak=MAX(best_streak,CASE WHEN ? THEN streak+1 ELSE best_streak END),"
       "last_active=date('now')", (uid, 1 if ok else 0, 1 if ok else 0, uid, ok, ok))
    if ok: add_xp(uid, 5)
    else: add_xp(uid, 1)
    add_activity(uid)
    await c.message.edit_reply_markup(reply_markup=None)
    text = "✅ <b>To'g'ri!</b>" if ok else f"❌ <b>Noto'g'ri!</b>\n✅ <b>{correct}</b>"
    btns = []
    for o in opts:
        lbl = f"✅ {o}" if o == correct else (f"❌ {o}" if o == opts[idx] and not ok else o)
        btns.append([btn(lbl, "noop")])
    await c.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    new_badges = get_badges(uid)
    for b in new_badges:
        await c.message.answer(f"🎉 <b>Yangi yutuq!</b> {b}")
    await c.answer()

# ===== STATS =====
@dp.message(Command("stats"))
@dp.message(F.text == "📊 Statistika")
async def stats_(m: Message):
    try:
        uid = m.from_user.id
        s = uq1("SELECT * FROM user_stats WHERE user_id=?", (uid,)) or {}
        acc = (s["correct"]/s["total"]*100) if s.get("total",0) > 0 else 0
        fav = uq1("SELECT COUNT(*) as c FROM user_favorites WHERE user_id=?", (uid,))
        badges = uq("SELECT badge FROM badges WHERE user_id=?", (uid,))
        seen = uq1("SELECT COUNT(*) as c FROM seen_words WHERE user_id=?", (uid,))["c"]
        act = uq1("SELECT COUNT(*) as c FROM user_activity WHERE user_id=?", (uid,))["c"]
        days = uq1("SELECT COUNT(DISTINCT date) as c FROM user_activity WHERE user_id=?", (uid,))["c"]
        await m.answer(
            f"<b>📊 Statistika</b>\n\n<b>👤 Siz:</b>\n"
            f"  📝 {s.get('total',0)} ta savol\n  ✅ {s.get('correct',0)} to'g'ri\n"
            f"  📈 {acc:.1f}% aniqlik\n  🔥 {s.get('streak',0)} kunlik seriya\n"
            f"  🏆 Eng yaxshi: {s.get('best_streak',0)}\n"
            f"  📚 {seen} ta so'z ko'rgan\n"
            f"  💬 {s.get('chat_count',0)} ta chat\n"
            f"  ⭐ {fav['c'] if fav else 0} sevimli\n"
            f"  🎮 {act} ta harakat\n"
            f"  📅 {days} kun faol\n"
            f"  ⚡ {s.get('xp',0)} XP\n"
            f"  🏅 {len(badges)} ta yutuq\n\n"
            f"<b>📚 Baza:</b>\n  📝 {word_count()} so'z\n"
            f"  📂 {topic_count()} mavzu\n  ❓ {quiz_count()} test",
            reply_markup=mk())
    except Exception as e: await m.answer(f"❌ {e}")

# ===== LEADERBOARD =====
@dp.message(Command("leaderboard"))
@dp.message(F.text == "🏅 Reyting")
async def lb(m: Message):
    try:
        rows = uq("SELECT user_id,total,correct,xp FROM user_stats ORDER BY xp DESC LIMIT 10")
        if not rows: return await m.answer("🏅 Reyting bo'sh.")
        lines = ["<b>🏅 Reyting (Top 10)</b>\n\n"]
        for i, r in enumerate(rows, 1):
            medal = ["🥇","🥈","🥉",""][i-1] if i <= 3 else ""
            lines.append(f"{medal} <b>#{i}</b>  {r['total']} test, {r['xp']} XP")
        await m.answer("\n".join(lines), reply_markup=mk())
    except Exception as e: await m.answer(f"❌ {e}")

# ===== BADGES =====
@dp.message(Command("badges"))
@dp.message(F.text == "🏆 Yutuqlar")
async def badges_(m: Message):
    try:
        earned = {r["badge"] for r in uq("SELECT badge FROM badges WHERE user_id=?", (m.from_user.id,))}
        all_badges = [
            (5, "📚 Yangi o'quvchi"), (25, "🎯 So'z ovchi"), (100, "⭐ Bilimdon"),
            (500, "🏆 Lug'at ustasi"), (1000, "👑 So'zlar qiroli"), (3000, "💎 Ensiklopediya"),
        ]
        lines = ["<b>🏆 Yutuqlar</b>\n"]
        for need, name in all_badges:
            status = "✅" if name in earned else "❌"
            s = f"{need} ta test"
            lines.append(f"{status} <b>{name}</b> — {s}")
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
                await m.answer(fmt_vocab(r, i), reply_markup=gkb(r["id"]))
    except Exception as e: await m.answer(f"❌ {e}")

@dp.callback_query(F.data.startswith("fav:"))
async def fav_cb(c: CallbackQuery):
    wid = int(c.data.split(":")[1])
    ux("INSERT OR IGNORE INTO user_favorites(user_id,word_id) VALUES(?,?)", (c.from_user.id, wid))
    await c.answer("⭐ Qo'shildi!", show_alert=False)

# ===== EXPORT =====
@dp.message(Command("export"))
async def export_(m: Message):
    favs = uq("SELECT word_id FROM user_favorites WHERE user_id=? ORDER BY added_at DESC", (m.from_user.id,))
    if not favs: return await m.answer("⭐ Sevimlilar bo'sh.")
    ids = [r["word_id"] for r in favs]
    rows = q(f"SELECT * FROM vocab_enriched WHERE id IN ({','.join('?'*len(ids))})", ids)
    text = "English Vocabulary Master — Sevimlilar\n" + "="*40 + "\n"
    for i, r in enumerate(rows, 1):
        text += f"\n{i}. {r['english']} — {r['uzbek']}\n   {r['definition']}\n"
    buf = io.BytesIO(text.encode("utf-8"))
    await m.answer_document(InputFile(buf, filename="favorites.txt"), caption="⭐ Sevimlilar ro'yxati")

# ===== REVIEW =====
@dp.message(Command("review"))
@dp.message(F.text == "🔄 Takrorlash")
async def review_(m: Message):
    uid = m.from_user.id
    try:
        rows = uq("SELECT word_id,wrong_count FROM seen_words WHERE user_id=? AND wrong_count>0 ORDER BY wrong_count DESC LIMIT 5", (uid,))
        if not rows:
            favs = uq("SELECT word_id FROM user_favorites WHERE user_id=? ORDER BY added_at DESC LIMIT 5", (uid,))
            if not favs: return await m.answer("🔄 Takrorlash uchun so'z yo'q.\nTest ishlang yoki sevimlilarga so'z qo'shing.")
            ids = [r["word_id"] for r in favs]
        else:
            ids = [r["word_id"] for r in rows]
        words = q(f"SELECT * FROM vocab_enriched WHERE id IN ({','.join('?'*len(ids))})", ids)
        await m.answer(f"🔄 <b>Takrorlash</b> ({len(words)} ta so'z):")
        for i, r in enumerate(words, 1):
            await m.answer(fmt_vocab(r, i), reply_markup=gkb(r["id"]))
    except Exception as e: await m.answer(f"❌ {e}")

# ===== WORD OF DAY =====
@dp.message(Command("word"))
@dp.message(F.text == "📖 Kundagi so'z")
async def wod(m: Message):
    try:
        row = get_wod()
        if not row: return await m.answer("❌ So'z topilmadi.")
        await m.answer(f"<b>📖 {date.today().isoformat()} — Kundagi so'z</b>\n\n{fmt_extended(row)}\n\n🤖 /gen {row['english']}", reply_markup=gkb(row["id"]))
    except Exception as e: await m.answer(f"❌ {e}")

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
        lines.append("\n<b>Buyruqlar:</b>\n/grammar_part1\n/grammar_part2\n/grammar_part3\n"
                     "/grammar_browse — Barcha patternlar\n/grammar_quiz\n/check_grammar + gap")
        await m.answer("\n".join(lines), reply_markup=mk())
    except Exception as e: await m.answer(f"❌ {e}")

# ===== GRAMMAR BROWSE =====
@dp.message(Command("grammar_browse"))
async def gbrowse(m: Message):
    try:
        patterns = q("SELECT * FROM grammar_patterns ORDER BY category, title_en")
        if not patterns: return await m.answer("Patterns topilmadi.")
        cats = defaultdict(list)
        for p in patterns:
            cats[p["category"]].append(p)
        lines = ["<b>📚 Barcha Grammar Patterns</b>\n"]
        for cat, pats in cats.items():
            lines.append(f"\n<b>{cat}</b> ({len(pats)}):")
            for p in pats:
                lines.append(f"  • <code>{p['title_en']}</code> — {p['level']}")
        # Split into multiple messages if needed
        full = "\n".join(lines)
        if len(full) > 4000:
            for i in range(0, len(full), 4000):
                await m.answer(full[i:i+4000])
        else:
            await m.answer(full)
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
        await m.answer(
            f"<b>📝 Grammar Quiz</b>\n\nPattern: {item['pattern_title']}\n"
            f"Formula: <code>{item['formula']}</code>\n\n<b>Question:</b>\n{item['question']}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn(o, f"gq:{item['id']}:{o}:{correct}")] for o in opts]))
    except Exception as e: await m.answer(f"❌ {e}")

@dp.callback_query(F.data.startswith("gq:"))
async def gq_cb(c: CallbackQuery):
    p = c.data.split(":", 3)
    ok = p[2] == p[3]
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer("✅ <b>Correct!</b>" if ok else f"❌ <b>Wrong!</b>\n✅ <b>{p[3]}</b>")
    await c.answer()

@dp.message(Command("check_grammar"))
async def check_(m: Message):
    p = m.text.split(maxsplit=1)
    if len(p) < 2: return await m.answer("📝 /check_grammar <sentence>\nMasalan: I go to school yesterday")
    sent = p[1].strip()
    if not OPENROUTER_API_KEY: return await m.answer("⚠️ OpenRouter API kaliti yo'q.")
    msg = await m.answer("⏳ Checking...")
    try:
        r = await gen_ai(f"""You are an IELTS English teacher. Analyze this sentence and return ONLY valid JSON:
Sentence: "{sent}"
{{"is_correct":true/false,"corrected_answer":"...","better_version":"...","explanation_uz":"...","score":8}}""", 0.3)
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
        if res.get("explanation_uz"): reply += f"\n<b>💡</b> {res['explanation_uz']}"
        await msg.edit_text(reply)
        add_xp(m.from_user.id, 3)
    except Exception as e: await msg.edit_text(f"❌ {e}")

# ===== CALLBACKS =====
@dp.callback_query(F.data.startswith("gen:"))
async def gen_cb(c: CallbackQuery):
    wid = int(c.data.split(":")[1])
    row = q1("SELECT * FROM vocab_enriched WHERE id=?", (wid,))
    if not row: return await c.answer("❌", show_alert=True)
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer(f"🤖 <b>{row['english']}</b> — rejim:", reply_markup=mkb(wid))

@dp.callback_query(F.data.startswith("mode:"))
async def mode_cb(c: CallbackQuery):
    p = c.data.split(":"); wid = int(p[1]); mode = p[2]
    row = q1("SELECT * FROM vocab_enriched WHERE id=?", (wid,))
    if not row: return await c.answer("❌", show_alert=True)
    await c.answer("⏳ AI...")
    await c.message.edit_reply_markup(reply_markup=None)
    r = await gen_ai(f"""Create ONE English sentence using "{row['english']}" ({row['uzbek']}).
Mode: {mode}. Topic: {row['topic']}, Level: {row['level']}
Return JSON: {{"sentence_en":"...","sentence_uz":"...","explanation_uz":"..."}}""")
    if not r: return await c.message.answer(f"❌ AI xatosi.\n/gen {row['english']}")
    if r.startswith("```"): r = r.split("\n",1)[1] if "\n" in r else r[3:]
    if r.endswith("```"): r = r.rsplit("```",1)[0]
    try:
        res = json.loads(r.strip())
        await c.message.answer(f"<b>🤖 AI ({mode})</b>\n\n<b>🇬🇧</b> {res.get('sentence_en','')}\n"
                               f"<b>🇺🇿</b> {res.get('sentence_uz','')}\n\n<b>💡</b> {res.get('explanation_uz','')}")
    except: await c.message.answer(r)

@dp.callback_query(F.data == "noop")
async def noop(c: CallbackQuery): await c.answer()

# ===== INLINE SEARCH =====
@dp.inline_query()
async def inline_query(q: types.InlineQuery):
    text = q.query.strip()
    if len(text) < 2: return await q.answer([types.InlineQueryResultArticle(
        id="help", title="So'zni kiriting...",
        input_message_content=types.InputTextMessageContent("🔍 English yoki Uzbek tilida so'z yozing"),
        description="2 ta belgidan ko'p bo'lishi kerak"
    )])
    pat = f"%{text}%"
    try:
        rows = q("SELECT * FROM vocab_enriched WHERE english LIKE ? OR uzbek LIKE ? LIMIT 15",
                 (pat, pat))
        results = []
        for i, r in enumerate(rows):
            content = fmt_vocab(r)
            results.append(types.InlineQueryResultArticle(
                id=str(r["id"]),
                title=f"{r['english']} — {r['uzbek']}",
                description=f"{r['topic']} | {r['level']} | {r['definition'][:80]}",
                input_message_content=types.InputTextMessageContent(content),
                reply_markup=gkb(r["id"])
            ))
        await q.answer(results[:20], cache_time=60, is_personal=True)
    except:
        await q.answer([], cache_time=60, is_personal=True)

# ===== ADMIN =====
@dp.message(Command("admin"))
async def admin(m: Message):
    if m.from_user.id not in ADMIN_IDS: return await m.answer("❌ Ruxsat yo'q.")
    users = uq1("SELECT COUNT(*) as c FROM user_stats")["c"]
    favs = uq1("SELECT COUNT(*) as c FROM user_favorites")["c"]
    act = uq1("SELECT COUNT(*) as c FROM user_activity")["c"]
    await m.answer(f"<b>👑 Admin Panel</b>\n\nUsers: {users}\nFavorites: {favs}\n"
                   f"Activities: {act}\nDB: {DB_PATH.stat().st_size/1024/1024:.1f} MB\n"
                   f"User DB: {Path(__file__).resolve().parent.joinpath('user_data.db').stat().st_size/1024:.1f} KB")

# ===== FALLBACK + AI CHAT =====
@dp.message()
async def fallback(m: Message):
    txt = m.text.strip()
    uid = m.from_user.id
    # AI Chat mode
    if uid in chat_sessions:
        chat_sessions[uid].append({"role":"user","content":txt})
        full = chat_sessions[uid][-8:]  # last 8 messages
        prompt = str(full)
        r = await gen_ai(f"""You are an English tutor. Previous conversation: {prompt}
Respond naturally, correct mistakes, explain in Uzbek when needed. Keep it under 200 words.""")
        if r:
            chat_sessions[uid].append({"role":"assistant","content":r})
            ux("UPDATE user_stats SET chat_count=chat_count+1 WHERE user_id=?", (uid,))
            add_xp(uid, 1)
            await m.answer(r)
        else:
            await m.answer("❌ AI xatosi. /chat_end va qayta boshlang.")
        return
    # Word lookup
    try:
        pat = f"%{txt}%"
        rows = q("SELECT * FROM vocab_enriched WHERE english LIKE ? OR uzbek LIKE ? OR definition LIKE ? "
                 "ORDER BY CASE WHEN english LIKE ? THEN 0 WHEN uzbek LIKE ? THEN 1 ELSE 2 END LIMIT 4",
                 (pat, pat, pat, txt, txt))
        if rows:
            await m.answer(f"🔍 <b>{txt}</b> — {len(rows)} ta:")
            for i, r in enumerate(rows, 1):
                ux("INSERT INTO seen_words(user_id,word_id,seen_count) VALUES(?,?,1) "
                   "ON CONFLICT(user_id,word_id) DO UPDATE SET seen_count=seen_count+1,last_seen=datetime('now')",
                   (uid, r["id"]))
                await m.answer(fmt_vocab(r, i), reply_markup=gkb(r["id"]))
            add_activity(uid)
        else:
            # Try AI
            r = await gen_ai(f"""User sent: "{txt}". This might be a question or message.
If it's an English-related question, answer helpfully in Uzbek.
If it's not clear, suggest /help. Keep it short.""", 0.5, 300)
            if r: await m.answer(r)
            else: await m.answer("🤔 Noto'g'ri buyruq.\n/help yoki tugmalardan foydalaning.", reply_markup=mk())
    except Exception as e:
        await m.answer(f"❌ {e}")

async def main():
    logger.info("🤖 Bot is starting...")
    await dp.start_polling(bot, handle_signals=False)

if __name__ == "__main__":
    asyncio.run(main())
