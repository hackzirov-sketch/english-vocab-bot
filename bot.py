import os, json, random, sqlite3, asyncio, threading, logging
from pathlib import Path
from datetime import date, datetime
from functools import lru_cache

import httpx
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
    Message, CallbackQuery,
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

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing in bot/.env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("vocab_bot")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ===================== MAIN DB =====================
_db_lock = threading.Lock()
_conn = None

def get_conn():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA cache_size=-8000")
    return _conn

def q_all(sql, params=None):
    with _db_lock:
        c = get_conn()
        return c.execute(sql, params).fetchall() if params else c.execute(sql).fetchall()

def q_one(sql, params=None):
    with _db_lock:
        c = get_conn()
        return c.execute(sql, params).fetchone() if params else c.execute(sql).fetchone()

# ===================== USER DATA DB =====================
_user_lock = threading.Lock()
_user_conn = None

def get_uconn():
    global _user_conn
    if _user_conn is None:
        p = Path(__file__).resolve().parent / "user_data.db"
        _user_conn = sqlite3.connect(str(p), check_same_thread=False)
        _user_conn.row_factory = sqlite3.Row
        _user_conn.execute("""CREATE TABLE IF NOT EXISTS user_stats (
            user_id INTEGER PRIMARY KEY, total INTEGER DEFAULT 0,
            correct INTEGER DEFAULT 0, streak INTEGER DEFAULT 0, best_streak INTEGER DEFAULT 0)""")
        _user_conn.execute("""CREATE TABLE IF NOT EXISTS user_favorites (
            user_id INTEGER, word_id INTEGER, added_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, word_id))""")
        _user_conn.execute("""CREATE TABLE IF NOT EXISTS word_of_day (
            date TEXT PRIMARY KEY, word_id INTEGER)""")
        _user_conn.commit()
    return _user_conn

def u_all(sql, params=None):
    with _user_lock:
        c = get_uconn()
        return c.execute(sql, params).fetchall() if params else c.execute(sql).fetchall()

def u_one(sql, params=None):
    with _user_lock:
        c = get_uconn()
        return c.execute(sql, params).fetchone() if params else c.execute(sql).fetchone()

def u_exec(sql, params=None):
    with _user_lock:
        c = get_uconn()
        if params: c.execute(sql, params)
        else: c.execute(sql)
        c.commit()

# ===================== CACHE =====================
_cache = {}
_CACHE_TTL = 300

def cached(ttl=_CACHE_TTL):
    def deco(fn):
        def wrapper(*a, **kw):
            key = fn.__name__
            now = datetime.now().timestamp()
            if key in _cache and now - _cache[key]["ts"] < ttl:
                return _cache[key]["val"]
            val = fn(*a, **kw)
            _cache[key] = {"val": val, "ts": now}
            return val
        return wrapper
    return deco

@cached()
def get_topics():
    return [dict(r) for r in q_all("SELECT topic, COUNT(*) as count FROM vocab_enriched GROUP BY topic ORDER BY topic")]

@cached()
def get_word_count():
    return q_one("SELECT COUNT(*) FROM vocab_enriched")[0]

@cached()
def get_topic_count():
    return q_one("SELECT COUNT(*) FROM topics")[0]

@cached()
def get_quiz_count():
    return q_one("SELECT COUNT(*) FROM quiz_items")[0]

@cached()
def get_type_dist():
    return q_all("SELECT type, COUNT(*) as count FROM vocab_enriched GROUP BY type ORDER BY count DESC")

def get_word_of_day():
    today = date.today().isoformat()
    r = u_one("SELECT word_id FROM word_of_day WHERE date = ?", (today,))
    if r:
        row = q_one("SELECT * FROM vocab_enriched WHERE id = ?", (r["word_id"],))
        if row: return row
    row = q_one("SELECT * FROM vocab_enriched ORDER BY RANDOM() LIMIT 1")
    if row:
        u_exec("INSERT OR REPLACE INTO word_of_day (date, word_id) VALUES (?, ?)", (today, row["id"]))
    return row

# ===================== KEYBOARDS =====================
def main_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Mavzular"), KeyboardButton(text="🏷 Turlar")],
            [KeyboardButton(text="🔍 Qidirish"), KeyboardButton(text="❓ Test")],
            [KeyboardButton(text="🤖 AI Jumla"), KeyboardButton(text="📊 Statistika")],
            [KeyboardButton(text="⭐ Sevimlilar"), KeyboardButton(text="📖 Kundagi so'z"), KeyboardButton(text="📚 Grammar")],
            [KeyboardButton(text="ℹ️ Yordam")],
        ], resize_keyboard=True
    )

def paginate(items, page, per_page, cb_prefix):
    start = page * per_page
    end = start + per_page
    chunk = items[start:end]
    buttons = []
    for i, item in enumerate(chunk):
        label = f"{item.get('topic', item.get('type', item.get('english', str(item))))}"
        if 'count' in item: label = f"{label} ({item['count']})"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"{cb_prefix}:{i + start}")])
    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"{cb_prefix}_page:{page - 1}"))
    if end < len(items): nav.append(InlineKeyboardButton(text="➡️", callback_data=f"{cb_prefix}_page:{page + 1}"))
    if nav: buttons.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def gen_kb(vocab_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 AI Jumla", callback_data=f"gen:{vocab_id}"),
         InlineKeyboardButton(text="⭐", callback_data=f"fav:{vocab_id}")]
    ])

def mode_kb(vocab_id):
    modes = [("☀️ Daily","daily"),("🎤 Speaking","speaking"),("✍️ Writing","writing"),
             ("📄 Essay","essay"),("💼 Formal","formal")]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"mode:{vocab_id}:{mode}")] for label, mode in modes
    ])

def quiz_kb(qid, opts, correct):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=o, callback_data=f"qa:{qid}:{i}:{correct}")] for i, o in enumerate(opts)
    ])

# ===================== HELPERS =====================
def fmt_vocab(row, idx=None):
    p = f"<b>{idx}.</b> " if idx else ""
    return (f"{p}<b>{row['english']}</b>\n  🇺🇿 {row['uzbek']}\n"
            f"  📂 <code>{row['topic']}</code> | 🏷 <code>{row['type']}</code> | 📊 {row['level']}\n"
            f"  📖 {row['definition']}\n  📝 <i>{row['example_en']}</i>\n  🇺🇿 {row['example_uz']}")

async def gen_ai(en, uz, topic, vtype, level, mode="speaking"):
    if not OPENROUTER_API_KEY: return None
    prompt = f"""You are an English teacher for Uzbek students. Create ONE natural English sentence using this word.
Word: {en}
Uzbek: {uz}
Topic: {topic}
Type: {vtype}
Level: {level}
Mode: {mode}
Rules: Natural, matches topic/level, give Uzbek translation, explain usage in Uzbek. Return ONLY valid JSON:
{{"sentence_en":"...","sentence_uz":"...","explanation_uz":"...","mode":"{mode}","word":"{en}"}}"""
    try:
        async with httpx.AsyncClient(timeout=25.0) as cl:
            r = await cl.post(f"{OPENROUTER_BASE_URL}/chat/completions",
                headers={"Authorization":f"Bearer {OPENROUTER_API_KEY}","Content-Type":"application/json"},
                json={"model":OPENROUTER_MODEL,"messages":[{"role":"user","content":prompt}],
                      "temperature":0.7,"max_tokens":512})
        if r.status_code != 200: return None
        txt = r.json()["choices"][0]["message"]["content"].strip()
        if txt.startswith("```"): txt = txt.split("\n",1)[1] if "\n" in txt else txt[3:]
        if txt.endswith("```"): txt = txt.rsplit("```",1)[0]
        return json.loads(txt.strip())
    except Exception as e:
        logger.error(f"AI gen error: {e}")
        return None

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
    ex = q_all("SELECT example_en,example_uz FROM grammar_examples WHERE pattern_id=? LIMIT 2", (p["id"],))
    if ex:
        lines.append("\n<b>Examples:</b>")
        for e in ex: lines.append(f"EN: {e['example_en']}\nUZ: {e['example_uz']}\n")
    return "\n".join(lines)

# ===================== HANDLERS =====================
@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "<b>📚 English Vocabulary Master</b>\n\n"
        "3480+ so'z, 24 mavzu, AI jumla yaratish\n\n"
        "Tugmalardan foydalaning:", reply_markup=main_kb())

@dp.message(Command("help"))
@dp.message(F.text == "ℹ️ Yordam")
async def help_cmd(m: Message):
    await m.answer(
        "<b>📋 Buyruqlar:</b>\n"
        "📋 /topics — Mavzular\n🏷 /types — Turlar\n"
        "🔍 /search &lt;so'z&gt; — Qidirish\n📂 /topic &lt;MAVZU&gt; — Mavzu bo'yicha\n"
        "❓ /quiz — Test\n🤖 /gen &lt;so'z&gt; — AI jumla\n📊 /stats — Statistika\n"
        "⭐ /fav — Sevimlilar\n📖 /word — Kundagi so'z\n"
        "📚 /grammar — Grammar\n/grammar_quiz — Grammar test\n"
        "/check_grammar + gap — AI grammar check", reply_markup=main_kb())

@dp.message(Command("topics"))
@dp.message(F.text == "📋 Mavzular")
async def topics(m: Message):
    try:
        t = get_topics()
        await m.answer(f"<b>📂 Mavzular</b> — {len(t)} ta, {get_word_count()} so'z\n\nTanlang:",
                       reply_markup=paginate(t, 0, 10, "tp"))
    except Exception as e: await m.answer(f"❌ Xatolik: {e}")

@dp.callback_query(F.data.startswith("tp_page:"))
async def tp_page(c: CallbackQuery):
    page = int(c.data.split(":")[1])
    await c.message.edit_reply_markup(reply_markup=paginate(get_topics(), page, 10, "tp"))
    await c.answer()

@dp.callback_query(F.data.startswith("tp:"))
async def tp_cb(c: CallbackQuery):
    idx = int(c.data.split(":")[1])
    topics = get_topics()
    if idx >= len(topics): return await c.answer("Topilmadi.", show_alert=True)
    topic = topics[idx]["topic"]
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=None)
    total = q_one("SELECT COUNT(*) FROM vocab_enriched WHERE topic = ?", (topic,))[0]
    rows = q_all("SELECT * FROM vocab_enriched WHERE topic = ? ORDER BY RANDOM() LIMIT 10", (topic,))
    if not rows: return await c.message.answer(f"❌ <code>{topic}</code> bo'sh.")
    await c.message.answer(f"📂 <b>{topic}</b> — {total} so'z:")
    for i, r in enumerate(rows, 1):
        await c.message.answer(fmt_vocab(r, i), reply_markup=gen_kb(r["id"]))

@dp.message(Command("topic"))
async def topic_cmd(m: Message):
    p = m.text.split(maxsplit=1)
    if len(p) < 2: return await m.answer("📂 /topic <MAVZU>\nMasalan: /topic Education")
    t = p[1].strip().upper()
    try:
        rows = q_all("SELECT * FROM vocab_enriched WHERE topic = ? ORDER BY RANDOM() LIMIT 10", (t,))
        if not rows: return await m.answer(f"❌ <code>{t}</code> bo'yicha so'z yo'q.")
        await m.answer(f"📂 <b>{t}</b> — {len(rows)} ta:")
        for i, r in enumerate(rows, 1): await m.answer(fmt_vocab(r, i), reply_markup=gen_kb(r["id"]))
    except Exception as e: await m.answer(f"❌ Xatolik: {e}")

@dp.message(Command("types"))
@dp.message(F.text == "🏷 Turlar")
async def types_cmd(m: Message):
    try:
        rows = get_type_dist()
        lines = ["<b>🏷 So'z turlari:</b>\n"]
        for r in rows: lines.append(f"  • <code>{r['type']}</code> — {r['count']} ta")
        await m.answer("\n".join(lines), reply_markup=main_kb())
    except Exception as e: await m.answer(f"❌ Xatolik: {e}")

@dp.message(F.text == "🔍 Qidirish")
async def search_prompt(m: Message):
    await m.answer("🔍 So'zni kiriting:\n<code>abandon</code> yoki <code>ish</code>")

@dp.message(Command("search"))
async def search_cmd(m: Message):
    p = m.text.split(maxsplit=1)
    if len(p) < 2: return await m.answer("🔍 /search <so'z>")
    q = p[1].strip()
    try:
        pat = f"%{q}%"
        rows = q_all("SELECT * FROM vocab_enriched WHERE english LIKE ? OR uzbek LIKE ? OR definition LIKE ? "
                     "ORDER BY CASE WHEN english LIKE ? THEN 0 WHEN uzbek LIKE ? THEN 1 ELSE 2 END LIMIT 8",
                     (pat, pat, pat, q, q))
        if not rows: return await m.answer(f"❌ '{q}' bo'yicha topilmadi.")
        await m.answer(f"🔍 <b>{q}</b> — {len(rows)} ta:")
        for i, r in enumerate(rows, 1):
            fav = u_one("SELECT 1 FROM user_favorites WHERE user_id=? AND word_id=?", (m.from_user.id, r["id"]))
            await m.answer(("⭐ " if fav else "") + fmt_vocab(r, i), reply_markup=gen_kb(r["id"]))
    except Exception as e: await m.answer(f"❌ Xatolik: {e}")

@dp.message(F.text == "🤖 AI Jumla")
async def gen_prompt(m: Message):
    await m.answer("🤖 So'zni kiriting:\n<code>abandon</code>")

@dp.message(Command("gen"))
async def gen_cmd(m: Message):
    p = m.text.split(maxsplit=1)
    if len(p) < 2: return await m.answer("🤖 /gen <so'z>")
    q = p[1].strip()
    try:
        row = q_one("SELECT * FROM vocab_enriched WHERE english=? OR english LIKE ? LIMIT 1", (q, f"%{q}%"))
        if not row: return await m.answer(f"❌ '<code>{q}</code>' topilmadi.")
        msg = await m.answer("⏳ AI yaratmoqda...")
        r = await gen_ai(row["english"], row["uzbek"], row["topic"], row["type"], row["level"])
        if not r: return await msg.edit_text("❌ AI xatosi. OpenRouter kalitini tekshiring.")
        await msg.edit_text(
            f"<b>🤖 AI Generated</b>\n\n<b>🇬🇧</b> {r.get('sentence_en','')}\n"
            f"<b>🇺🇿</b> {r.get('sentence_uz','')}\n\n<b>💡</b> {r.get('explanation_uz','')}")
    except Exception as e: await m.answer(f"❌ Xatolik: {e}")

@dp.message(Command("quiz"))
@dp.message(F.text == "❓ Test")
async def quiz_cmd(m: Message):
    p = m.text.split(maxsplit=1)
    q = p[1].strip().upper() if len(p) > 1 and not p[1].startswith("/") else None
    try:
        row = q_one("SELECT * FROM quiz_items ORDER BY RANDOM() LIMIT 1") if not q else \
              q_one("SELECT * FROM quiz_items WHERE topic=? ORDER BY RANDOM() LIMIT 1", (q,))
        if not row: return await m.answer(f"❌ Test topilmadi.")
        correct = row["correct_answer"]
        wrong = parse_wrong(row["wrong_answers_json"])
        opts = [correct] + wrong[:3]
        random.shuffle(opts)
        await m.answer(f"<b>❓ Savol:</b>\n{row['question']}", reply_markup=quiz_kb(row["id"], opts, correct))
    except Exception as e: await m.answer(f"❌ Xatolik: {e}")

@dp.callback_query(F.data.startswith("qa:"))
async def qa_cb(c: CallbackQuery):
    p = c.data.split(":")
    idx = int(p[2]); correct = p[3]
    row = q_one("SELECT * FROM quiz_items WHERE id=?", (p[1],))
    if not row: return await c.answer("Savol topilmadi.", show_alert=True)
    wrong = parse_wrong(row["wrong_answers_json"])
    opts = [correct] + wrong[:3]; random.shuffle(opts)
    ok = opts[idx] == correct
    uid = c.from_user.id
    u_exec("INSERT INTO user_stats(user_id,total,correct,streak,best_streak) VALUES(?,1,?,?,?) "
           "ON CONFLICT(user_id) DO UPDATE SET total=total+1,correct=correct+?,"
           "streak=CASE WHEN ? THEN streak+1 ELSE 0 END,"
           "best_streak=MAX(best_streak,CASE WHEN ? THEN streak+1 ELSE best_streak END)",
           (uid, 1 if ok else 0, 1 if ok else 0, 1 if ok else 0, ok, ok))
    await c.message.edit_reply_markup(reply_markup=None)
    text = "✅ <b>To'g'ri!</b>" if ok else f"❌ <b>Noto'g'ri!</b>\n✅ <b>{correct}</b>"
    btns = []
    for o in opts:
        lbl = f"✅ {o}" if o == correct else (f"❌ {o}" if o == opts[idx] and not ok else o)
        btns.append([InlineKeyboardButton(text=lbl, callback_data="noop")])
    await c.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    await c.answer()

@dp.message(Command("stats"))
@dp.message(F.text == "📊 Statistika")
async def stats_cmd(m: Message):
    try:
        s = u_one("SELECT * FROM user_stats WHERE user_id=?", (m.from_user.id,))
        s = s or {"total":0,"correct":0,"streak":0,"best_streak":0}
        acc = (s["correct"]/s["total"]*100) if s["total"] > 0 else 0
        favs = u_one("SELECT COUNT(*) as cnt FROM user_favorites WHERE user_id=?", (m.from_user.id,))
        await m.answer(
            f"<b>📊 Statistika</b>\n\n<b>👤 Siz:</b>\n"
            f"  📝 {s['total']} ta savol\n  ✅ {s['correct']} to'g'ri\n"
            f"  📈 {acc:.1f}% aniqlik\n  🔥 {s['streak']} kunlik seriya\n"
            f"  🏆 Eng yaxshi: {s['best_streak']}\n"
            f"  ⭐ {(favs['cnt'] if favs else 0)} sevimli\n\n"
            f"<b>📚 Baza:</b>\n  📝 {get_word_count()} so'z\n"
            f"  📂 {get_topic_count()} mavzu\n  ❓ {get_quiz_count()} test",
            reply_markup=main_kb())
    except Exception as e: await m.answer(f"❌ Xatolik: {e}")

@dp.message(Command("fav"))
@dp.message(F.text == "⭐ Sevimlilar")
async def fav_cmd(m: Message):
    favs = u_all("SELECT word_id FROM user_favorites WHERE user_id=? ORDER BY added_at DESC", (m.from_user.id,))
    if not favs: return await m.answer("⭐ Sevimlilar bo'sh.\nSo'z ustidagi ⭐ tugmasini bosing.")
    ids = [r["word_id"] for r in favs]
    try:
        for cs in range(0, len(ids), 10):
            chunk = ids[cs:cs+10]
            rows = q_all(f"SELECT * FROM vocab_enriched WHERE id IN ({','.join('?'*len(chunk))})", chunk)
            for i, r in enumerate(rows, cs+1):
                await m.answer(fmt_vocab(r, i), reply_markup=gen_kb(r["id"]))
    except Exception as e: await m.answer(f"❌ Xatolik: {e}")

@dp.callback_query(F.data.startswith("fav:"))
async def fav_cb(c: CallbackQuery):
    wid = int(c.data.split(":")[1])
    u_exec("INSERT OR IGNORE INTO user_favorites(user_id,word_id) VALUES(?,?)", (c.from_user.id, wid))
    await c.answer("⭐ Qo'shildi!", show_alert=False)

@dp.message(Command("addfav"))
async def addfav(m: Message):
    p = m.text.split(maxsplit=1)
    if len(p) < 2: return await m.answer("/addfav <id>")
    try:
        u_exec("INSERT OR IGNORE INTO user_favorites(user_id,word_id) VALUES(?,?)", (m.from_user.id, int(p[1])))
        await m.answer(f"✅ Qo'shildi!")
    except: await m.answer("❌ Noto'g'ri ID.")

@dp.message(Command("rmfav"))
async def rmfav(m: Message):
    p = m.text.split(maxsplit=1)
    if len(p) < 2: return await m.answer("/rmfav <id>")
    try:
        u_exec("DELETE FROM user_favorites WHERE user_id=? AND word_id=?", (m.from_user.id, int(p[1])))
        await m.answer(f"✅ O'chirildi.")
    except: await m.answer("❌ Noto'g'ri ID.")

@dp.message(Command("word"))
@dp.message(F.text == "📖 Kundagi so'z")
async def word_day(m: Message):
    try:
        row = get_word_of_day()
        if not row: return await m.answer("❌ So'z topilmadi.")
        await m.answer(f"<b>📖 {date.today().isoformat()} — Kundagi so'z</b>\n\n{fmt_vocab(row)}\n\n🤖 /gen {row['english']}",
                       reply_markup=gen_kb(row["id"]))
    except Exception as e: await m.answer(f"❌ Xatolik: {e}")

@dp.message(Command("grammar"))
@dp.message(F.text == "📚 Grammar")
async def grammar(m: Message):
    try:
        sections = q_all("SELECT * FROM grammar_sections ORDER BY display_order")
        if not sections: return await m.answer("Grammar topilmadi.")
        lines = ["<b>📚 Grammar Sections</b>\n"]
        for s in sections:
            pc = q_one("SELECT COUNT(*) FROM grammar_patterns WHERE section_code=?", (s["code"],))[0]
            lines.append(f"  • <b>{s['title_en']}</b> — {pc} patterns")
        lines.append("\n<b>Buyruqlar:</b>\n/grammar_part1\n/grammar_part2\n/grammar_part3\n"
                     "/grammar_quiz\n/check_grammar + gap")
        await m.answer("\n".join(lines), reply_markup=main_kb())
    except Exception as e: await m.answer(f"❌ Xatolik: {e}")

@dp.message(Command("grammar_part1"))
async def gp1(m: Message):
    try:
        p = q_one("SELECT * FROM grammar_patterns WHERE ielts_part='Part 1' OR section_code='PART1_SHORT' ORDER BY RANDOM() LIMIT 1")
        if not p: return await m.answer("Part 1 topilmadi.")
        await m.answer(fmt_grammar(p, "IELTS Part 1 Grammar"))
    except Exception as e: await m.answer(f"❌ Xatolik: {e}")

@dp.message(Command("grammar_part2"))
async def gp2(m: Message):
    try:
        p = q_one("SELECT * FROM grammar_patterns WHERE ielts_part='Part 2' OR section_code='PART2_STORY' ORDER BY RANDOM() LIMIT 1")
        if not p: return await m.answer("Part 2 topilmadi.")
        await m.answer(fmt_grammar(p, "IELTS Part 2 Story Grammar"))
    except Exception as e: await m.answer(f"❌ Xatolik: {e}")

@dp.message(Command("grammar_part3"))
async def gp3(m: Message):
    try:
        p = q_one("SELECT * FROM grammar_patterns WHERE ielts_part='Part 3' OR section_code='PART3_DISCUSSION' ORDER BY RANDOM() LIMIT 1")
        if not p: return await m.answer("Part 3 topilmadi.")
        await m.answer(fmt_grammar(p, "IELTS Part 3 Discussion Grammar"))
    except Exception as e: await m.answer(f"❌ Xatolik: {e}")

@dp.message(Command("grammar_quiz"))
async def gq(m: Message):
    try:
        item = q_one("SELECT qi.*, gp.title_en as pattern_title, gp.formula FROM grammar_quiz_items qi "
                     "JOIN grammar_patterns gp ON gp.id=qi.pattern_id ORDER BY RANDOM() LIMIT 1")
        if not item: return await m.answer("Grammar quiz topilmadi.")
        correct = item["correct_answer"]
        wrong = parse_wrong(item["wrong_answers_json"])
        opts = [correct] + wrong[:3]; random.shuffle(opts)
        btns = [[InlineKeyboardButton(text=o, callback_data=f"gq:{item['id']}:{o}:{correct}")] for o in opts]
        await m.answer(
            f"<b>📝 Grammar Quiz</b>\n\nPattern: {item['pattern_title']}\n"
            f"Formula: <code>{item['formula']}</code>\n\n<b>Question:</b>\n{item['question']}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    except Exception as e: await m.answer(f"❌ Xatolik: {e}")

@dp.callback_query(F.data.startswith("gq:"))
async def gq_cb(c: CallbackQuery):
    p = c.data.split(":", 3)
    ok = p[2] == p[3]
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer("✅ <b>Correct!</b>" if ok else f"❌ <b>Wrong!</b>\n✅ <b>{p[3]}</b>")
    await c.answer()

@dp.message(Command("check_grammar"))
async def check_grammar(m: Message):
    p = m.text.split(maxsplit=1)
    if len(p) < 2: return await m.answer("📝 /check_grammar <sentence>\nExample: I go to school yesterday")
    sent = p[1].strip()
    if not OPENROUTER_API_KEY: return await m.answer("⚠️ OpenRouter kaliti yo'q.")
    msg = await m.answer("⏳ Checking...")
    try:
        prompt = f"""You are an IELTS English teacher. Analyze this sentence and return ONLY valid JSON:
Sentence: "{sent}"
{{"is_correct":true/false,"corrected_answer":"...","better_version":"...","explanation_uz":"...","score":8}}"""
        async with httpx.AsyncClient(timeout=30.0) as cl:
            r = await cl.post(f"{OPENROUTER_BASE_URL}/chat/completions",
                headers={"Authorization":f"Bearer {OPENROUTER_API_KEY}","Content-Type":"application/json"},
                json={"model":OPENROUTER_MODEL,"messages":[{"role":"user","content":prompt}],
                      "temperature":0.3,"max_tokens":800})
        if r.status_code != 200: return await msg.edit_text("❌ API xatosi.")
        txt = r.json()["choices"][0]["message"]["content"].strip()
        if txt.startswith("```"): txt = txt.split("\n",1)[1] if "\n" in txt else txt[3:]
        if txt.endswith("```"): txt = txt.rsplit("```",1)[0]
        res = json.loads(txt.strip())
        ok = res.get("is_correct", False)
        reply = f"{'✅' if ok else '❌'} <b>Grammar Check</b>\n\n<b>You:</b> {sent}\n"
        reply += f"<b>Status:</b> {'Correct' if ok else 'Needs work'}\n"
        reply += f"<b>Score:</b> {res.get('score',0)}/10\n\n"
        if res.get("corrected_answer"): reply += f"<b>Corrected:</b> {res['corrected_answer']}\n"
        if res.get("better_version"): reply += f"<b>Better:</b> {res['better_version']}\n"
        if res.get("explanation_uz"): reply += f"\n<b>💡</b> {res['explanation_uz']}"
        await msg.edit_text(reply)
    except Exception as e: await msg.edit_text(f"❌ Xatolik: {e}")

@dp.callback_query(F.data.startswith("gen:"))
async def gen_cb(c: CallbackQuery):
    wid = int(c.data.split(":")[1])
    row = q_one("SELECT * FROM vocab_enriched WHERE id=?", (wid,))
    if not row: return await c.answer("Topilmadi.", show_alert=True)
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer(f"🤖 <b>{row['english']}</b> — rejim tanlang:", reply_markup=mode_kb(wid))

@dp.callback_query(F.data.startswith("mode:"))
async def mode_cb(c: CallbackQuery):
    p = c.data.split(":")
    wid = int(p[1]); mode = p[2]
    row = q_one("SELECT * FROM vocab_enriched WHERE id=?", (wid,))
    if not row: return await c.answer("Topilmadi.", show_alert=True)
    await c.answer("⏳ AI yaratmoqda...")
    await c.message.edit_reply_markup(reply_markup=None)
    r = await gen_ai(row["english"], row["uzbek"], row["topic"], row["type"], row["level"], mode)
    if not r: return await c.message.answer(f"❌ AI xatosi.\n/gen {row['english']}")
    await c.message.answer(
        f"<b>🤖 AI Generated ({mode})</b>\n\n<b>🇬🇧</b> {r.get('sentence_en','')}\n"
        f"<b>🇺🇿</b> {r.get('sentence_uz','')}\n\n<b>💡</b> {r.get('explanation_uz','')}")

@dp.callback_query(F.data == "noop")
async def noop(c: CallbackQuery): await c.answer()

@dp.message(Command("admin"))
async def admin(m: Message):
    if m.from_user.id not in ADMIN_IDS: return await m.answer("❌ Ruxsat yo'q.")
    users = u_one("SELECT COUNT(*) as cnt FROM user_stats")[0]
    favs = u_one("SELECT COUNT(*) as cnt FROM user_favorites")[0]
    await m.answer(f"<b>👑 Admin Panel</b>\n\n<b>Users:</b> {users}\n<b>Favorites:</b> {favs}\n"
                   f"<b>DB:</b> {DB_PATH.stat().st_size/1024/1024:.1f} MB")

@dp.message()
async def fallback(m: Message):
    txt = m.text.strip()
    try:
        pat = f"%{txt}%"
        rows = q_all("SELECT * FROM vocab_enriched WHERE english LIKE ? OR uzbek LIKE ? OR definition LIKE ? "
                     "ORDER BY CASE WHEN english LIKE ? THEN 0 WHEN uzbek LIKE ? THEN 1 ELSE 2 END LIMIT 3",
                     (pat, pat, pat, txt, txt))
        if rows:
            await m.answer(f"🔍 <b>{txt}</b> — {len(rows)} ta:")
            for i, r in enumerate(rows, 1): await m.answer(fmt_vocab(r, i), reply_markup=gen_kb(r["id"]))
        else:
            await m.answer("🤔 Noto'g'ri buyruq.\n/help yoki tugmalardan foydalaning.", reply_markup=main_kb())
    except Exception as e: await m.answer(f"❌ Xatolik: {e}")

async def main():
    logger.info("🤖 Bot starting...")
    await dp.start_polling(bot, handle_signals=False)

if __name__ == "__main__":
    asyncio.run(main())
