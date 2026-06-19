import os
import json
import random
import sqlite3
import asyncio
import threading
from pathlib import Path
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
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
DB_PATH = Path(__file__).resolve().parent / "database" / "master_maximal_v14_openrouter_ready.db"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Add it to bot/.env")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# --- Thread-safe persistent DB ---
_db_lock = threading.Lock()
_db_conn = None

def get_db():
    global _db_conn
    if _db_conn is None:
        _db_conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _db_conn.row_factory = sqlite3.Row
    return _db_conn

def db_execute(query, params=None):
    with _db_lock:
        conn = get_db()
        if params:
            return conn.execute(query, params).fetchall()
        return conn.execute(query).fetchall()

def db_execute_one(query, params=None):
    with _db_lock:
        conn = get_db()
        if params:
            return conn.execute(query, params).fetchone()
        return conn.execute(query).fetchone()

# --- In-memory caches ---
_cache_topics = None
_cache_word_count = 0
_cache_quiz_count = 0
_cache_topic_count = 0

def invalidate_cache():
    global _cache_topics, _cache_word_count, _cache_quiz_count, _cache_topic_count
    _cache_topics = None
    _cache_word_count = 0
    _cache_quiz_count = 0
    _cache_topic_count = 0

def get_cached_topics():
    global _cache_topics
    if _cache_topics is None:
        rows = db_execute(
            "SELECT topic, COUNT(*) as count FROM vocab_enriched GROUP BY topic ORDER BY topic"
        )
        _cache_topics = [dict(r) for r in rows]
    return _cache_topics

def get_cached_word_count():
    global _cache_word_count
    if _cache_word_count == 0:
        _cache_word_count = db_execute_one("SELECT COUNT(*) FROM vocab_enriched")[0]
    return _cache_word_count

def get_cached_topic_count():
    global _cache_topic_count
    if _cache_topic_count == 0:
        _cache_topic_count = db_execute_one("SELECT COUNT(*) FROM topics")[0]
    return _cache_topic_count

def get_cached_quiz_count():
    global _cache_quiz_count
    if _cache_quiz_count == 0:
        _cache_quiz_count = db_execute_one("SELECT COUNT(*) FROM quiz_items")[0]
    return _cache_quiz_count

# --- User data ---
user_favorites: dict[int, list[int]] = {}
user_stats: dict[int, dict] = {}
_fav_lock = threading.Lock()
_stats_lock = threading.Lock()


def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Mavzular"), KeyboardButton(text="🏷 Turlar")],
            [KeyboardButton(text="🔍 Qidirish"), KeyboardButton(text="❓ Test")],
            [KeyboardButton(text="🤖 AI Jumla"), KeyboardButton(text="📊 Statistika")],
            [KeyboardButton(text="⭐ Sevimlilar"), KeyboardButton(text="📖 Kundagi so'z"), KeyboardButton(text="📚 Grammar")],
            [KeyboardButton(text="ℹ️ Yordam")],
        ],
        resize_keyboard=True,
    )


def quiz_keyboard(question_id, options, correct):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=opt, callback_data=f"qa:{question_id}:{i}:{correct}")]
        for i, opt in enumerate(options)
    ])


def topic_keyboard(topics):
    buttons, row = [], []
    for i, t in enumerate(topics):
        row.append(InlineKeyboardButton(text=f"{t['topic']} ({t['count']})", callback_data=f"tp:{t['topic']}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def gen_keyboard(vocab_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 AI Jumla", callback_data=f"gen:{vocab_id}")]
    ])


def mode_keyboard(vocab_id):
    modes = [("☀️ Daily", "daily"), ("🎤 Speaking", "speaking"), ("✍️ Writing", "writing"),
             ("📄 Essay", "essay"), ("💼 Formal", "formal")]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"mode:{vocab_id}:{mode}")]
        for label, mode in modes
    ])


def format_vocab(row, index=None):
    prefix = f"<b>{index}.</b> " if index else ""
    return (
        f"{prefix}<b>{row['english']}</b>\n"
        f"  🇺🇿 {row['uzbek']}\n"
        f"  📂 Topic: {row['topic']}\n"
        f"  🏷 Type: {row['type']}\n"
        f"  📊 Level: {row['level']}\n"
        f"  📖 {row['definition']}\n"
        f"  📝 <i>{row['example_en']}</i>\n"
        f"  🇺🇿 {row['example_uz']}"
    )


async def generate_ai(english, uzbek, topic, vtype, level, mode="speaking"):
    if not OPENROUTER_API_KEY:
        return None
    prompt = f"""You are an English teacher for Uzbek students.

Create one natural English sentence using this word or phrase.

Word/Phrase: {english}
Uzbek meaning: {uzbek}
Topic: {topic}
Type: {vtype}
Level: {level}
Mode: {mode}

Rules:
1. The sentence must be natural and useful.
2. The sentence must match the topic.
3. The sentence must match the level.
4. Do not make the sentence too long.
5. Do not use strange or rare grammar.
6. After the English sentence, give Uzbek translation.
7. Then explain in Uzbek how the word is used.
8. Return only valid JSON.

JSON format:
{{
  "sentence_en": "...",
  "sentence_uz": "...",
  "explanation_uz": "...",
  "mode": "...",
  "word": "..."
}}"""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{OPENROUTER_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
                json={"model": OPENROUTER_MODEL, "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.7, "max_tokens": 512},
            )
        if resp.status_code != 200:
            return None
        content = resp.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content.rsplit("```", 1)[0]
        return json.loads(content.strip())
    except Exception:
        return None


def update_user_stats(user_id, correct=None):
    with _stats_lock:
        if user_id not in user_stats:
            user_stats[user_id] = {"total": 0, "correct": 0, "streak": 0, "best_streak": 0}
        s = user_stats[user_id]
        s["total"] += 1
        if correct:
            s["correct"] += 1
            s["streak"] += 1
            if s["streak"] > s["best_streak"]:
                s["best_streak"] = s["streak"]
        else:
            s["streak"] = 0


# ==================== HANDLERS ====================

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "<b>📚 English Vocabulary Master Bot</b>\n\n"
        "Salom! Bu bot sizga ingliz so'zlari va iboralarini o'rganishga yordam beradi.\n\n"
        "Quyidagi tugmalardan foydalaning yoki buyruqlar kiriting:",
        reply_markup=main_keyboard()
    )


@dp.message(Command("help"))
@dp.message(F.text == "ℹ️ Yordam")
async def cmd_help(message: Message):
    await message.answer(
        "<b>📋 Buyruqlar ro'yxati:</b>\n\n"
        "📋 /topics — Barcha mavzular\n"
        "🏷 /types — So'z turlari\n"
        "🔍 /search &lt;word&gt; — So'z qidirish\n"
        "📂 /topic &lt;TOPIC&gt; — Mavzudan so'zlar\n"
        "❓ /quiz — Tasodifiy test\n"
        "❓ /quiz &lt;TOPIC&gt; — Mavzudan test\n"
        "🤖 /gen &lt;word&gt; — AI jumla yaratish\n"
        "📊 /stats — Statistika\n"
        "⭐ /fav — Sevimlilar\n"
        "📖 /word — Kundagi so'z\n\n"
        "<b>📚 Grammar:</b>\n"
        "  /grammar — Grammar sections\n"
        "  /grammar_part1 — Part 1 pattern\n"
        "  /grammar_part2 — Part 2 pattern\n"
        "  /grammar_part3 — Part 3 pattern\n"
        "  /grammar_quiz — Grammar quiz\n"
        "  /check_grammar + sentence — AI grammar check\n\n"
        "<b>💡 Tugmalardan ham foydalaning!</b>",
        reply_markup=main_keyboard()
    )


# ===== TOPICS =====
@dp.message(Command("topics"))
@dp.message(F.text == "📋 Mavzular")
async def cmd_topics(message: Message):
    try:
        topics = get_cached_topics()
        total = get_cached_word_count()
        kb = topic_keyboard(topics)
        await message.answer(
            f"<b>📂 Mavzular</b> ({len(topics)} ta, {total} so'z)\n\nMavzuni tanlang:",
            reply_markup=kb
        )
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}")


@dp.callback_query(F.data.startswith("tp:"))
async def cb_topic(callback: CallbackQuery):
    topic = callback.data.split(":", 1)[1]
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    try:
        total = db_execute_one("SELECT COUNT(*) FROM vocab_enriched WHERE topic = ?", (topic,))[0]
        rows = db_execute("SELECT * FROM vocab_enriched WHERE topic = ? ORDER BY RANDOM() LIMIT 8", (topic,))
        if not rows:
            await callback.message.answer(f"❌ '<code>{topic}</code>' mavzusida so'zlar topilmadi.")
            return
        await callback.message.answer(f"📂 <b>{topic}</b> — {total} so'z, {len(rows)} tasodifiy:")
        for i, r in enumerate(rows, 1):
            await callback.message.answer(format_vocab(r, i), reply_markup=gen_keyboard(r['id']))
    except Exception as e:
        await callback.message.answer(f"❌ Xatolik: {e}")


@dp.message(Command("topic"))
async def cmd_topic(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("📂 Foydalanish: /topic <MAVZU>\nMasalan: /topic Education")
        return
    topic = parts[1].strip().upper()
    try:
        rows = db_execute("SELECT * FROM vocab_enriched WHERE topic = ? ORDER BY RANDOM() LIMIT 8", (topic,))
        if not rows:
            await message.answer(f"❌ '<code>{topic}</code>' mavzusida so'zlar topilmadi.")
            return
        await message.answer(f"📂 <b>{topic}</b> — {len(rows)} ta so'z:")
        for i, r in enumerate(rows, 1):
            await message.answer(format_vocab(r, i), reply_markup=gen_keyboard(r['id']))
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}")


# ===== TYPES =====
@dp.message(Command("types"))
@dp.message(F.text == "🏷 Turlar")
async def cmd_types(message: Message):
    try:
        rows = db_execute("SELECT type, COUNT(*) as count FROM vocab_enriched GROUP BY type ORDER BY count DESC")
        lines = ["<b>🏷 So'z turlari:</b>\n"]
        for r in rows:
            lines.append(f"  • <code>{r['type']}</code> — {r['count']} ta")
        await message.answer("\n".join(lines), reply_markup=main_keyboard())
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}")


# ===== SEARCH =====
@dp.message(F.text == "🔍 Qidirish")
async def cmd_search_prompt(message: Message):
    await message.answer(
        "🔍 Qidirish uchun so'z kiriting:\nMasalan: <code>abandon</code> yoki <code>kvartira</code>",
        reply_markup=main_keyboard()
    )


@dp.message(Command("search"))
async def cmd_search(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("🔍 Foydalanish: /search <word>\nMasalan: /search abandon")
        return
    query = parts[1].strip()
    try:
        pattern = f"%{query}%"
        rows = db_execute(
            "SELECT * FROM vocab_enriched WHERE english LIKE ? OR uzbek LIKE ? OR definition LIKE ? "
            "ORDER BY CASE WHEN english LIKE ? THEN 0 WHEN uzbek LIKE ? THEN 1 ELSE 2 END LIMIT 5",
            (pattern, pattern, pattern, query, query)
        )
        if not rows:
            await message.answer(f"❌ '{query}' bo'yicha hech narsa topilmadi.")
            return
        await message.answer(f"🔍 <b>{query}</b> — {len(rows)} ta natija:")
        for i, r in enumerate(rows, 1):
            with _fav_lock:
                fav = "⭐" if message.from_user.id in user_favorites and r['id'] in user_favorites.get(message.from_user.id, []) else ""
            await message.answer(format_vocab(r, i) + f"\n\n{fav}", reply_markup=gen_keyboard(r['id']))
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}")


# ===== AI Generate =====
@dp.message(F.text == "🤖 AI Jumla")
async def cmd_gen_prompt(message: Message):
    await message.answer(
        "🤖 AI jumla yaratish uchun so'z kiriting:\nMasalan: <code>abandon</code>",
        reply_markup=main_keyboard()
    )


@dp.message(Command("gen"))
async def cmd_gen(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("🤖 Foydalanish: /gen <word>\nMasalan: /gen abandon")
        return
    query = parts[1].strip()
    try:
        row = db_execute_one("SELECT * FROM vocab_enriched WHERE english = ? OR english LIKE ? LIMIT 1", (query, f"%{query}%"))
        if not row:
            await message.answer(f"❌ '<code>{query}</code>' so'zi topilmadi.")
            return
        msg = await message.answer("⏳ AI jumlani yaratmoqda...")
        result = await generate_ai(row["english"], row["uzbek"], row["topic"], row["type"], row["level"])
        if not result:
            await msg.edit_text("❌ AI generatsiya amalga oshirilmadi.\nOpenRouter API kalitini tekshiring.")
            return
        await msg.edit_text(
            f"<b>🤖 AI Generated Sentence</b>\n\n"
            f"<b>🇬🇧 English:</b>\n{result.get('sentence_en', '')}\n\n"
            f"<b>🇺🇿 Uzbek:</b>\n{result.get('sentence_uz', '')}\n\n"
            f"<b>💡 Tushuntirish:</b>\n{result.get('explanation_uz', '')}\n\n"
            f"📝 Mode: {result.get('mode', '')}"
        )
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}")


# ===== QUIZ =====
@dp.message(Command("quiz"))
@dp.message(F.text == "❓ Test")
async def cmd_quiz(message: Message):
    parts = message.text.split(maxsplit=1)
    topic = parts[1].strip().upper() if len(parts) > 1 and not parts[1].strip().startswith("/") else None
    try:
        if topic:
            row = db_execute_one("SELECT * FROM quiz_items WHERE topic = ? ORDER BY RANDOM() LIMIT 1", (topic,))
        else:
            row = db_execute_one("SELECT * FROM quiz_items ORDER BY RANDOM() LIMIT 1")
        if not row:
            msg = f"❌ '<code>{topic}</code>' mavzusida test topilmadi." if topic else "❌ Test savollari topilmadi."
            await message.answer(msg, reply_markup=main_keyboard())
            return
        correct = row["correct_answer"]
        qid = row["id"]
        wrong = []
        try:
            raw = row["wrong_answers_json"]
            if raw:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    wrong = [str(x) for x in parsed]
        except (json.JSONDecodeError, TypeError):
            pass
        options = [correct] + wrong[:3]
        random.shuffle(options)
        await message.answer(f"<b>❓ Savol:</b>\n{row['question']}", reply_markup=quiz_keyboard(qid, options, correct))
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}")


@dp.callback_query(F.data.startswith("qa:"))
async def cb_quiz_answer(callback: CallbackQuery):
    parts = callback.data.split(":")
    qid = parts[1]
    selected_idx = int(parts[2])
    correct_answer = parts[3]
    try:
        row = db_execute_one("SELECT * FROM quiz_items WHERE id = ?", (qid,))
        if not row:
            await callback.answer("Savol topilmadi.", show_alert=True)
            return
        wrong = []
        try:
            raw = row["wrong_answers_json"]
            if raw:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    wrong = [str(x) for x in parsed]
        except (json.JSONDecodeError, TypeError):
            pass
        options = [correct_answer] + wrong[:3]
        random.shuffle(options)
        selected = options[selected_idx]
        is_correct = selected == correct_answer
        update_user_stats(callback.from_user.id, is_correct)
        result_text = "✅ <b>To'g'ri!</b>" if is_correct else f"❌ <b>Noto'g'ri!</b>\n✅ Javob: <b>{correct_answer}</b>"
        buttons = []
        for i, opt in enumerate(options):
            if opt == correct_answer:
                buttons.append([InlineKeyboardButton(text=f"✅ {opt}", callback_data="noop")])
            elif opt == selected and not is_correct:
                buttons.append([InlineKeyboardButton(text=f"❌ {opt}", callback_data="noop")])
            else:
                buttons.append([InlineKeyboardButton(text=opt, callback_data="noop")])
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(result_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()
    except Exception as e:
        await callback.answer("Xatolik yuz berdi.", show_alert=True)


# ===== STATS =====
@dp.message(Command("stats"))
@dp.message(F.text == "📊 Statistika")
async def cmd_stats(message: Message):
    try:
        with _stats_lock:
            s = user_stats.get(message.from_user.id, {"total": 0, "correct": 0, "streak": 0, "best_streak": 0})
        accuracy = (s["correct"] / s["total"] * 100) if s["total"] > 0 else 0
        with _fav_lock:
            fav_count = len(user_favorites.get(message.from_user.id, []))
        total_words = get_cached_word_count()
        total_topics = get_cached_topic_count()
        total_quiz = get_cached_quiz_count()
        await message.answer(
            f"<b>📊 Statistika</b>\n\n"
            f"<b>👤 Sizning natijalaringiz:</b>\n"
            f"  📝 Jami savollar: {s['total']}\n"
            f"  ✅ To'g'ri: {s['correct']}\n"
            f"  📈 Aniqlik: {accuracy:.1f}%\n"
            f"  🔥 Joriy seriya: {s['streak']}\n"
            f"  🏆 Eng yaxshi seriya: {s['best_streak']}\n"
            f"  ⭐ Sevimlilar: {fav_count}\n\n"
            f"<b>📚 Bazada:</b>\n"
            f"  📝 So'zlar: {total_words}\n"
            f"  📂 Mavzular: {total_topics}\n"
            f"  ❓ Testlar: {total_quiz}",
            reply_markup=main_keyboard()
        )
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}")


# ===== FAVORITES =====
@dp.message(Command("fav"))
@dp.message(F.text == "⭐ Sevimlilar")
async def cmd_favorites(message: Message):
    with _fav_lock:
        favs = user_favorites.get(message.from_user.id, [])
    if not favs:
        await message.answer(
            "⭐ Sevimlilar ro'yxati bo'sh.\n\n"
            "Sevimlilarga qo'shish uchun /addfav <id> buyrug'ini ishlating.",
            reply_markup=main_keyboard()
        )
        return
    try:
        placeholders = ",".join("?" * len(favs))
        # Split into chunks of 10 to avoid long messages
        for chunk_start in range(0, len(favs), 10):
            chunk = favs[chunk_start:chunk_start + 10]
            ph = ",".join("?" * len(chunk))
            rows = db_execute(f"SELECT * FROM vocab_enriched WHERE id IN ({ph})", chunk)
            for i, r in enumerate(rows, chunk_start + 1):
                await message.answer(format_vocab(r, i), reply_markup=gen_keyboard(r['id']))
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}")


@dp.message(Command("addfav"))
async def cmd_add_favorite(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Foydalanish: /addfav <word_id>")
        return
    try:
        word_id = int(parts[1].strip())
    except ValueError:
        await message.answer("❌ Noto'g'ri ID. Raqam kiriting.")
        return
    with _fav_lock:
        if message.from_user.id not in user_favorites:
            user_favorites[message.from_user.id] = []
        if word_id not in user_favorites[message.from_user.id]:
            user_favorites[message.from_user.id].append(word_id)
            await message.answer(f"✅ So'z #{word_id} sevimlilarga qo'shildi!")
        else:
            await message.answer("ℹ️ Bu so'z allaqachon sevimlilarda.")


@dp.message(Command("rmfav"))
async def cmd_remove_favorite(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Foydalanish: /rmfav <word_id>")
        return
    try:
        word_id = int(parts[1].strip())
    except ValueError:
        await message.answer("❌ Noto'g'ri ID. Raqam kiriting.")
        return
    with _fav_lock:
        if message.from_user.id in user_favorites and word_id in user_favorites[message.from_user.id]:
            user_favorites[message.from_user.id].remove(word_id)
            await message.answer(f"✅ So'z #{word_id} sevimlillardan o'chirildi.")
        else:
            await message.answer("ℹ️ Bu so'z sevimlillarda topilmadi.")


# ===== WORD OF DAY =====
@dp.message(Command("word"))
@dp.message(F.text == "📖 Kundagi so'z")
async def cmd_word_of_day(message: Message):
    try:
        row = db_execute_one("SELECT * FROM vocab_enriched ORDER BY RANDOM() LIMIT 1")
        if not row:
            await message.answer("❌ So'z topilmadi.")
            return
        await message.answer(
            f"<b>📖 Kundagi so'z</b>\n\n{format_vocab(row)}\n\n🤖 AI jumla uchun: /gen {row['english']}",
            reply_markup=gen_keyboard(row['id'])
        )
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}")


# ===== GRAMMAR =====
@dp.message(Command("grammar"))
@dp.message(F.text == "📚 Grammar")
async def cmd_grammar(message: Message):
    try:
        sections = db_execute("SELECT * FROM grammar_sections ORDER BY display_order")
        if not sections:
            await message.answer("📚 Grammatika bo'limlari topilmadi.")
            return
        lines = ["<b>📚 Grammar Sections</b>\n"]
        for s in sections:
            pc = db_execute_one("SELECT COUNT(*) FROM grammar_patterns WHERE section_code = ?", (s["code"],))[0]
            lines.append(f"  • <b>{s['title_en']}</b>")
            lines.append(f"    {s['title_uz']} ({pc} patterns)")
        lines.append("\n<b>Commands:</b>")
        lines.append("  /grammar_part1 — Part 1")
        lines.append("  /grammar_part2 — Part 2")
        lines.append("  /grammar_part3 — Part 3")
        lines.append("  /grammar_quiz — Grammar quiz")
        lines.append("  /check_grammar + sentence — AI check")
        await message.answer("\n".join(lines), reply_markup=main_keyboard())
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}")


def _format_grammar_pattern(pattern, section_title=None):
    lines = []
    if section_title:
        lines.append(f"<b>📚 {section_title}</b>\n")
    lines.append(f"<b>{pattern['title_en']}</b>")
    lines.append(f"🇺🇿 {pattern['title_uz']}")
    lines.append("")
    lines.append(f"<b>Level:</b> {pattern['level']}")
    lines.append(f"<b>Category:</b> {pattern['category']}")
    if "ielts_part" in pattern and pattern["ielts_part"]:
        lines.append(f"<b>IELTS Part:</b> {pattern['ielts_part']}")
    lines.append("")
    lines.append(f"<b>Formula:</b>")
    lines.append(f"<code>{pattern['formula']}</code>")
    lines.append("")
    lines.append(f"<b>{pattern['meaning_uz']}</b>")
    lines.append("")
    lines.append(f"{pattern['explanation_uz'][:400]}")
    if "when_to_use_uz" in pattern and pattern["when_to_use_uz"]:
        lines.append("")
        lines.append(f"<b>When to use:</b>")
        lines.append(f"{pattern['when_to_use_uz'][:300]}")
    try:
        examples = db_execute("SELECT example_en, example_uz FROM grammar_examples WHERE pattern_id = ? LIMIT 2", (pattern["id"],))
        if examples:
            lines.append("")
            lines.append("<b>Examples:</b>")
            for ex in examples:
                lines.append(f"EN: {ex['example_en']}")
                lines.append(f"UZ: {ex['example_uz']}")
                lines.append("")
    except Exception:
        pass
    return "\n".join(lines)


@dp.message(Command("grammar_part1"))
async def cmd_grammar_part1(message: Message):
    try:
        pattern = db_execute_one(
            "SELECT * FROM grammar_patterns WHERE ielts_part = 'Part 1' OR section_code = 'PART1_SHORT' ORDER BY RANDOM() LIMIT 1"
        )
        if not pattern:
            await message.answer("No Part 1 grammar found.")
            return
        await message.answer(_format_grammar_pattern(pattern, "IELTS Part 1 Grammar"), parse_mode=ParseMode.HTML)
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}")


@dp.message(Command("grammar_part2"))
async def cmd_grammar_part2(message: Message):
    try:
        pattern = db_execute_one(
            "SELECT * FROM grammar_patterns WHERE ielts_part = 'Part 2' OR section_code = 'PART2_STORY' ORDER BY RANDOM() LIMIT 1"
        )
        if not pattern:
            await message.answer("No Part 2 grammar found.")
            return
        await message.answer(_format_grammar_pattern(pattern, "IELTS Part 2 Story Grammar"), parse_mode=ParseMode.HTML)
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}")


@dp.message(Command("grammar_part3"))
async def cmd_grammar_part3(message: Message):
    try:
        pattern = db_execute_one(
            "SELECT * FROM grammar_patterns WHERE ielts_part = 'Part 3' OR section_code = 'PART3_DISCUSSION' ORDER BY RANDOM() LIMIT 1"
        )
        if not pattern:
            await message.answer("No Part 3 grammar found.")
            return
        await message.answer(_format_grammar_pattern(pattern, "IELTS Part 3 Discussion Grammar"), parse_mode=ParseMode.HTML)
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}")


# ===== GRAMMAR QUIZ =====
@dp.message(Command("grammar_quiz"))
async def cmd_grammar_quiz(message: Message):
    try:
        item = db_execute_one(
            "SELECT qi.*, gp.title_en as pattern_title, gp.formula "
            "FROM grammar_quiz_items qi JOIN grammar_patterns gp ON gp.id = qi.pattern_id "
            "ORDER BY RANDOM() LIMIT 1"
        )
        if not item:
            await message.answer("No grammar quiz found.")
            return
        correct = item["correct_answer"]
        wrong = []
        try:
            raw = item["wrong_answers_json"]
            if raw:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    wrong = [str(x) for x in parsed]
        except (json.JSONDecodeError, TypeError):
            pass
        options = [correct] + wrong[:3]
        random.shuffle(options)
        buttons = [[InlineKeyboardButton(text=opt, callback_data=f"gq:{item['id']}:{opt}:{correct}")] for opt in options]
        await message.answer(
            f"<b>📝 Grammar Quiz</b>\n\nPattern: {item['pattern_title']}\nFormula: <code>{item['formula']}</code>\n\n"
            f"<b>Question:</b>\n{item['question']}\n",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}")


@dp.callback_query(F.data.startswith("gq:"))
async def cb_grammar_quiz(callback: CallbackQuery):
    parts = callback.data.split(":", 3)
    selected = parts[2]
    correct = parts[3]
    is_correct = selected == correct
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "✅ <b>Correct!</b>" if is_correct else f"❌ <b>Wrong!</b>\n✅ Answer: <b>{correct}</b>",
        parse_mode=ParseMode.HTML
    )
    await callback.answer()


# ===== CHECK GRAMMAR =====
@dp.message(Command("check_grammar"))
async def cmd_check_grammar(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Foydalanish: /check_grammar <your sentence>\nExample: /check_grammar I go to school yesterday")
        return
    user_sentence = parts[1].strip()
    msg = await message.answer("⏳ Checking grammar with AI...")
    if not OPENROUTER_API_KEY:
        await msg.edit_text(
            f"<b>Your sentence:</b>\n{user_sentence}\n\n⚠️ OpenRouter API kaliti o'rnatilmagan.",
            parse_mode=ParseMode.HTML
        )
        return
    try:
        prompt = f"""You are an expert IELTS English teacher for Uzbek students.
Analyze this English sentence and return ONLY valid JSON:
Sentence: "{user_sentence}"
Return this exact JSON structure:
{{
  "is_correct": true or false,
  "corrected_answer": "corrected version if wrong",
  "better_version": "improved version at B2 level",
  "explanation_uz": "Uzbek explanation of the error",
  "used_grammar": ["grammar structures used"],
  "score": 8
}}"""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{OPENROUTER_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
                json={"model": OPENROUTER_MODEL, "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.3, "max_tokens": 800},
            )
        if resp.status_code != 200:
            await msg.edit_text("❌ OpenRouter API xatosi.")
            return
        content = resp.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content.rsplit("```", 1)[0]
        result = json.loads(content.strip())
        is_correct = result.get("is_correct", False)
        icon = "✅" if is_correct else "❌"
        reply = (
            f"{icon} <b>Grammar Check Result</b>\n\n<b>Your sentence:</b>\n{user_sentence}\n\n"
            f"<b>Status:</b> {'Correct!' if is_correct else 'Needs Improvement'}\n"
            f"<b>Score:</b> {result.get('score', 0)}/10\n\n"
        )
        if result.get("corrected_answer"):
            reply += f"<b>Corrected:</b>\n{result['corrected_answer']}\n\n"
        if result.get("better_version"):
            reply += f"<b>Better version:</b>\n{result['better_version']}\n\n"
        if result.get("explanation_uz"):
            reply += f"<b>Explanation (UZ):</b>\n{result['explanation_uz']}\n"
        await msg.edit_text(reply, parse_mode=ParseMode.HTML)
    except Exception as e:
        await msg.edit_text(f"❌ Xatolik: {e}")


# ===== GENERATION CALLBACKS =====
@dp.callback_query(F.data.startswith("gen:"))
async def cb_gen(callback: CallbackQuery):
    vocab_id = int(callback.data.split(":")[1])
    try:
        row = db_execute_one("SELECT * FROM vocab_enriched WHERE id = ?", (vocab_id,))
        if not row:
            await callback.answer("❌ So'z topilmadi.", show_alert=True)
            return
        await callback.answer()
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"🤖 <b>{row['english']}</b> uchun generation rejimini tanlang:",
            reply_markup=mode_keyboard(vocab_id)
        )
    except Exception as e:
        await callback.answer("Xatolik yuz berdi.", show_alert=True)


@dp.callback_query(F.data.startswith("mode:"))
async def cb_mode(callback: CallbackQuery):
    parts = callback.data.split(":")
    vocab_id = int(parts[1])
    mode = parts[2]
    try:
        row = db_execute_one("SELECT * FROM vocab_enriched WHERE id = ?", (vocab_id,))
        if not row:
            await callback.answer("❌ So'z topilmadi.", show_alert=True)
            return
        await callback.answer("⏳ AI jumlani yaratmoqda...")
        await callback.message.edit_reply_markup(reply_markup=None)
        result = await generate_ai(row["english"], row["uzbek"], row["topic"], row["type"], row["level"], mode)
        if not result:
            await callback.message.answer(f"❌ AI generatsiya amalga oshirilmadi.\nQayta urinib ko'ring: /gen {row['english']}")
            return
        await callback.message.answer(
            f"<b>🤖 AI Generated Sentence</b>\n\n"
            f"<b>🇬🇧 English:</b>\n{result.get('sentence_en', '')}\n\n"
            f"<b>🇺🇿 Uzbek:</b>\n{result.get('sentence_uz', '')}\n\n"
            f"<b>💡 Tushuntirish:</b>\n{result.get('explanation_uz', '')}\n\n"
            f"📝 Mode: {result.get('mode', '')}"
        )
    except Exception as e:
        await callback.message.answer(f"❌ Xatolik: {e}")


@dp.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()


# ===== FALLBACK =====
@dp.message()
async def handle_text(message: Message):
    text = message.text.strip()
    try:
        pattern = f"%{text}%"
        rows = db_execute(
            "SELECT * FROM vocab_enriched WHERE english LIKE ? OR uzbek LIKE ? OR definition LIKE ? "
            "ORDER BY CASE WHEN english LIKE ? THEN 0 WHEN uzbek LIKE ? THEN 1 ELSE 2 END LIMIT 3",
            (pattern, pattern, pattern, text, text)
        )
        if rows:
            await message.answer(f"🔍 '<b>{text}</b>' bo'yicha topilganlar:")
            for i, r in enumerate(rows, 1):
                await message.answer(format_vocab(r, i), reply_markup=gen_keyboard(r['id']))
        else:
            await message.answer(
                "🤔 Noto'g'ru buyruq yoki so'z.\nYordam uchun /help yoki tugmalardan foydalaning.",
                reply_markup=main_keyboard()
            )
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}")


async def main():
    print("🤖 Bot is starting...")
    await dp.start_polling(bot, handle_signals=False)


if __name__ == "__main__":
    asyncio.run(main())
