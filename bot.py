import os
import json
import random
import sqlite3
import asyncio
from pathlib import Path

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
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000")
DB_PATH = Path(__file__).resolve().parent / "database" / "master_maximal_v14_openrouter_ready.db"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Add it to bot/.env")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

user_favorites: dict[int, list[int]] = {}
user_stats: dict[int, dict] = {}


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📋 Mavzular"),
                KeyboardButton(text="🏷 Turlar"),
            ],
            [
                KeyboardButton(text="🔍 Qidirish"),
                KeyboardButton(text="❓ Test"),
            ],
            [
                KeyboardButton(text="🤖 AI Jumla"),
                KeyboardButton(text="📊 Statistika"),
            ],
            [
                KeyboardButton(text="⭐ Sevimlilar"),
                KeyboardButton(text="📖 Kundagi so'z"),
                KeyboardButton(text="📚 Grammar"),
            ],
            [
                KeyboardButton(text="ℹ️ Yordam"),
            ],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def quiz_keyboard(question_id, options, correct):
    buttons = []
    for i, opt in enumerate(options):
        buttons.append([InlineKeyboardButton(
            text=opt,
            callback_data=f"qa:{question_id}:{i}:{correct}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def topic_keyboard(topics):
    buttons = []
    row = []
    for i, t in enumerate(topics):
        row.append(InlineKeyboardButton(
            text=f"{t['topic']} ({t['count']})",
            callback_data=f"tp:{t['topic']}"
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def gen_keyboard(vocab_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Generation", callback_data=f"gen:{vocab_id}")]
    ])


def mode_keyboard(vocab_id):
    modes = [
        ("☀️ Daily", "daily"),
        ("🎤 Speaking", "speaking"),
        ("✍️ Writing", "writing"),
        ("📄 Essay", "essay"),
        ("💼 Formal", "formal"),
    ]
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
    if not GROQ_API_KEY:
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
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": GROQ_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.7,
                    "max_tokens": 512,
                },
            )
        if resp.status_code != 200:
            return None
        content = resp.json()["choices"][0]["message"]["content"]
        text = content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        return json.loads(text.strip())
    except Exception:
        return None


def update_user_stats(user_id, correct=None):
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


@dp.message(CommandStart())
async def cmd_start(message: Message):
    text = (
        "<b>📚 English Vocabulary Master Bot</b>\n\n"
        "Salom! Bu bot sizga ingliz so'zlari va iboralarini o'rganishga yordam beradi.\n\n"
        "Quyidagi tugmalardan foydalaning yoki buyruqlar kiriting:"
    )
    await message.answer(text, reply_markup=main_keyboard())


@dp.message(Command("help"))
async def cmd_help(message: Message):
    text = (
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
        "/grammar — Grammar sections\n"
        "/grammar_part1 — Part 1 pattern\n"
        "/grammar_part2 — Part 2 pattern\n"
        "/grammar_part3 — Part 3 pattern\n"
        "/grammar_quiz — Grammar quiz\n"
        "/check_grammar + sentence — AI grammar check\n\n"
        "<b>💡 Tugmalardan ham foydalaning!</b>"
    )
    await message.answer(text, reply_markup=main_keyboard())


@dp.message(F.text == "📋 Mavzular")
@dp.message(Command("topics"))
async def cmd_topics(message: Message):
    conn = get_db()
    rows = conn.execute(
        "SELECT topic, COUNT(*) as count FROM vocab_enriched GROUP BY topic ORDER BY topic"
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM vocab_enriched").fetchone()[0]
    conn.close()

    kb = topic_keyboard([{"topic": r["topic"], "count": r["count"]} for r in rows])

    text = f"<b>📂 Mavzular</b> ({len(rows)} ta, {total} so'z)\n\nMavzoni tanlang:"
    await message.answer(text, reply_markup=kb)


@dp.message(F.text == "🏷 Turlar")
@dp.message(Command("types"))
async def cmd_types(message: Message):
    conn = get_db()
    rows = conn.execute(
        "SELECT type, COUNT(*) as count FROM vocab_enriched GROUP BY type ORDER BY count DESC"
    ).fetchall()
    conn.close()

    lines = ["<b>🏷 So'z turlari:</b>\n"]
    for r in rows:
        lines.append(f"  • <code>{r['type']}</code> — {r['count']} ta")
    await message.answer("\n".join(lines), reply_markup=main_keyboard())


@dp.message(F.text == "🔍 Qidirish")
async def cmd_search_prompt(message: Message):
    await message.answer(
        "🔍 Qidirish uchun so'z kiriting:\n"
        "Masalan: <code>abandon</code> yoki <code>kvartira</code>",
        reply_markup=main_keyboard(),
    )


@dp.message(Command("search"))
async def cmd_search(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "🔍 Foydalanish: /search <word>\n"
            "Masalan: /search abandon"
        )
        return

    query = parts[1].strip()
    conn = get_db()
    pattern = f"%{query}%"
    rows = conn.execute(
        """SELECT * FROM vocab_enriched
           WHERE english LIKE ? OR uzbek LIKE ? OR definition LIKE ?
           ORDER BY CASE WHEN english LIKE ? THEN 0 WHEN uzbek LIKE ? THEN 1 ELSE 2 END
           LIMIT 5""",
        (pattern, pattern, pattern, query, query),
    ).fetchall()
    conn.close()

    if not rows:
        await message.answer(f"❌ '{query}' bo'yicha hech narsa topilmadi.")
        return

    await message.answer(f"🔍 <b>{query}</b> — {len(rows)} ta natija:")
    for i, r in enumerate(rows, 1):
        fav_text = "⭐" if message.from_user.id in user_favorites and r['id'] in user_favorites.get(message.from_user.id, []) else ""
        await message.answer(
            format_vocab(r, i) + f"\n\n{fav_text}",
            reply_markup=gen_keyboard(r['id']),
        )


@dp.message(F.text == "🤖 AI Jumla")
async def cmd_gen_prompt(message: Message):
    await message.answer(
        "🤖 AI jumla yaratish uchun so'z kiriting:\n"
        "Masalan: <code>abandon</code>",
        reply_markup=main_keyboard(),
    )


@dp.message(Command("gen"))
async def cmd_gen(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "🤖 Foydalanish: /gen <word>\n"
            "Masalan: /gen abandon"
        )
        return

    query = parts[1].strip()
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM vocab_enriched WHERE english = ? OR english LIKE ? LIMIT 1",
        (query, f"%{query}%"),
    ).fetchone()
    conn.close()

    if not row:
        await message.answer(f"❌ '<code>{query}</code>' so'zi topilmadi.")
        return

    msg = await message.answer("⏳ AI jumlani yaratmoqda...")

    result = await generate_ai(
        row["english"], row["uzbek"], row["topic"], row["type"], row["level"]
    )

    if not result:
        await msg.edit_text(
            "❌ AI generatsiya amalga oshirilmadi.\n"
            "Iltimos, qayta urinib ko'ring."
        )
        return

    text = (
        f"<b>🤖 AI Generated Sentence</b>\n\n"
        f"<b>🇬🇧 English:</b>\n{result.get('sentence_en', '')}\n\n"
        f"<b>🇺🇿 Uzbek:</b>\n{result.get('sentence_uz', '')}\n\n"
        f"<b>💡 Tushuntirish:</b>\n{result.get('explanation_uz', '')}\n\n"
        f"📝 Mode: {result.get('mode', '')}"
    )
    await msg.edit_text(text)


@dp.message(F.text == "❓ Test")
@dp.message(Command("quiz"))
async def cmd_quiz(message: Message):
    parts = message.text.split(maxsplit=1)
    topic = parts[1].strip().upper() if len(parts) > 1 and not parts[1].strip().startswith("/") else None

    conn = get_db()
    if topic:
        row = conn.execute(
            "SELECT * FROM quiz_items WHERE topic = ? ORDER BY RANDOM() LIMIT 1",
            (topic,),
        ).fetchone()
    else:
        row = conn.execute("SELECT * FROM quiz_items ORDER BY RANDOM() LIMIT 1").fetchone()
    conn.close()

    if not row:
        msg = f"❌ '<code>{topic}</code>' mavzusida test topilmadi." if topic else "❌ Test savollari topilmadi."
        await message.answer(msg, reply_markup=main_keyboard())
        return

    question = row["question"]
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

    kb = quiz_keyboard(qid, options, correct)
    await message.answer(f"<b>❓ Savol:</b>\n{question}", reply_markup=kb)


@dp.callback_query(F.data.startswith("qa:"))
async def cb_quiz_answer(callback: CallbackQuery):
    parts = callback.data.split(":")
    qid = parts[1]
    selected_idx = int(parts[2])
    correct_answer = parts[3]

    conn = get_db()
    row = conn.execute("SELECT * FROM quiz_items WHERE id = ?", (qid,)).fetchone()
    conn.close()

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
        text = opt
        if opt == correct_answer:
            text = f"✅ {opt}"
        elif opt == selected and not is_correct:
            text = f"❌ {opt}"
        buttons.append([InlineKeyboardButton(text=text, callback_data="noop")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(result_text, reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()


@dp.message(F.text == "📊 Statistika")
@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    user_id = message.from_user.id
    s = user_stats.get(user_id, {"total": 0, "correct": 0, "streak": 0, "best_streak": 0})
    accuracy = (s["correct"] / s["total"] * 100) if s["total"] > 0 else 0
    fav_count = len(user_favorites.get(user_id, []))

    conn = get_db()
    total_words = conn.execute("SELECT COUNT(*) FROM vocab_enriched").fetchone()[0]
    total_topics = conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
    total_quiz = conn.execute("SELECT COUNT(*) FROM quiz_items").fetchone()[0]
    conn.close()

    text = (
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
        f"  ❓ Testlar: {total_quiz}"
    )
    await message.answer(text, reply_markup=main_keyboard())


@dp.message(F.text == "⭐ Sevimlilar")
@dp.message(Command("fav"))
async def cmd_favorites(message: Message):
    user_id = message.from_user.id
    favs = user_favorites.get(user_id, [])

    if not favs:
        await message.answer(
            "⭐ Sevimlilar ro'yxati bo'sh.\n\n"
            "So'z qidirganda 🤖 Generation tugmasi orqali so'zlarni ko'ring.\n"
            "Sevimlilarga qo'shish uchun /addfav <id> buyrug'ini ishlating.",
            reply_markup=main_keyboard(),
        )
        return

    conn = get_db()
    placeholders = ",".join("?" * len(favs))
    rows = conn.execute(
        f"SELECT * FROM vocab_enriched WHERE id IN ({placeholders})",
        favs,
    ).fetchall()
    conn.close()

    await message.answer(f"⭐ <b>Sevimlilar</b> ({len(rows)} ta):")
    for i, r in enumerate(rows, 1):
        await message.answer(
            format_vocab(r, i),
            reply_markup=gen_keyboard(r['id']),
        )


@dp.message(Command("addfav"))
async def cmd_add_favorite(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Foydalanish: /addfav <word_id>")
        return

    try:
        word_id = int(parts[1].strip())
    except ValueError:
        await message.answer("❌ Noto'g'ri ID.raqam kiriting.")
        return

    user_id = message.from_user.id
    if user_id not in user_favorites:
        user_favorites[user_id] = []

    if word_id not in user_favorites[user_id]:
        user_favorites[user_id].append(word_id)
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
        await message.answer("❌ Noto'g'ri ID.raqam kiriting.")
        return

    user_id = message.from_user.id
    if user_id in user_favorites and word_id in user_favorites[user_id]:
        user_favorites[user_id].remove(word_id)
        await message.answer(f"✅ So'z #{word_id} sevimlillardan o'chirildi.")
    else:
        await message.answer("ℹ️ Bu so'z sevimlillarda topilmadi.")


@dp.message(F.text == "📖 Kundagi so'z")
@dp.message(Command("word"))
async def cmd_word_of_day(message: Message):
    conn = get_db()
    row = conn.execute("SELECT * FROM vocab_enriched ORDER BY RANDOM() LIMIT 1").fetchone()
    conn.close()

    if not row:
        await message.answer("❌ So'z topilmadi.")
        return

    text = (
        f"<b>📖 Kundagi so'z</b>\n\n"
        f"{format_vocab(row)}\n\n"
        f"🤖 AI jumla uchun: /gen {row['english']}"
    )
    await message.answer(text, reply_markup=gen_keyboard(row['id']))


@dp.message(Command("grammar"))
async def cmd_grammar(message: Message):
    conn = get_db()
    sections = conn.execute(
        "SELECT * FROM grammar_sections ORDER BY display_order"
    ).fetchall()
    conn.close()

    if not sections:
        await message.answer("📚 Grammatika bo'limlari topilmadi.")
        return

    lines = ["<b>📚 Grammar Sections</b>\n"]
    for s in sections:
        code = s["code"]
        title_en = s["title_en"] or ""
        title_uz = s["title_uz"] or ""
        conn = get_db()
        pc = conn.execute("SELECT COUNT(*) FROM grammar_patterns WHERE section_code = ?", (code,)).fetchone()[0]
        conn.close()
        lines.append(f"  • <b>{title_en}</b>")
        lines.append(f"    {title_uz} ({pc} patterns)")

    lines.append("\n<b>Commands:</b>")
    lines.append("/grammar_part1 — Part 1 grammar")
    lines.append("/grammar_part2 — Part 2 grammar")
    lines.append("/grammar_part3 — Part 3 grammar")
    lines.append("/grammar_quiz — Grammar quiz")
    lines.append("/check_grammar + your sentence — AI check")

    await message.answer("\n".join(lines), reply_markup=main_keyboard())


@dp.message(Command("grammar_part1"))
async def cmd_grammar_part1(message: Message):
    conn = get_db()
    pattern = conn.execute(
        "SELECT * FROM grammar_patterns WHERE ielts_part = 'Part 1' OR section_code = 'PART1_SHORT' ORDER BY RANDOM() LIMIT 1"
    ).fetchone()
    conn.close()

    if not pattern:
        await message.answer("No Part 1 grammar found.")
        return

    text = _format_grammar_pattern(pattern, "IELTS Part 1 Grammar")
    await message.answer(text, parse_mode=ParseMode.HTML)


@dp.message(Command("grammar_part2"))
async def cmd_grammar_part2(message: Message):
    conn = get_db()
    pattern = conn.execute(
        "SELECT * FROM grammar_patterns WHERE ielts_part = 'Part 2' OR section_code = 'PART2_STORY' ORDER BY RANDOM() LIMIT 1"
    ).fetchone()
    conn.close()

    if not pattern:
        await message.answer("No Part 2 grammar found.")
        return

    text = _format_grammar_pattern(pattern, "IELTS Part 2 Story Grammar")
    await message.answer(text, parse_mode=ParseMode.HTML)


@dp.message(Command("grammar_part3"))
async def cmd_grammar_part3(message: Message):
    conn = get_db()
    pattern = conn.execute(
        "SELECT * FROM grammar_patterns WHERE ielts_part = 'Part 3' OR section_code = 'PART3_DISCUSSION' ORDER BY RANDOM() LIMIT 1"
    ).fetchone()
    conn.close()

    if not pattern:
        await message.answer("No Part 3 grammar found.")
        return

    text = _format_grammar_pattern(pattern, "IELTS Part 3 Discussion Grammar")
    await message.answer(text, parse_mode=ParseMode.HTML)


@dp.message(Command("grammar_quiz"))
async def cmd_grammar_quiz(message: Message):
    conn = get_db()
    item = conn.execute(
        "SELECT qi.*, gp.title_en as pattern_title, gp.formula "
        "FROM grammar_quiz_items qi "
        "JOIN grammar_patterns gp ON gp.id = qi.pattern_id "
        "ORDER BY RANDOM() LIMIT 1"
    ).fetchone()
    conn.close()

    if not item:
        await message.answer("No grammar quiz found.")
        return

    correct = item["correct_answer"]
    wrong_raw = item["wrong_answers_json"] or "[]"
    try:
        wrong = json.loads(wrong_raw)
        if not isinstance(wrong, list):
            wrong = []
    except (json.JSONDecodeError, TypeError):
        wrong = []

    options = [correct] + wrong[:3]
    random.shuffle(options)

    text = (
        f"<b>📝 Grammar Quiz</b>\n\n"
        f"Pattern: {item['pattern_title']}\n"
        f"Formula: <code>{item['formula']}</code>\n\n"
        f"<b>Question:</b>\n{item['question']}\n"
    )

    buttons = []
    for opt in options:
        cb_data = f"gq:{item['id']}:{opt}:{correct}"
        buttons.append([InlineKeyboardButton(text=opt, callback_data=cb_data)])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(F.data.startswith("gq:"))
async def cb_grammar_quiz(callback: CallbackQuery):
    parts = callback.data.split(":", 3)
    qid = parts[1]
    selected = parts[2]
    correct = parts[3]

    is_correct = selected == correct

    result_text = "✅ <b>Correct!</b>" if is_correct else f"❌ <b>Wrong!</b>\n✅ Answer: <b>{correct}</b>"

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(result_text, parse_mode=ParseMode.HTML)
    await callback.answer()


@dp.message(Command("check_grammar"))
async def cmd_check_grammar(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Foydalanish: /check_grammar <your sentence>\n"
            "Example: /check_grammar I go to school yesterday"
        )
        return

    user_sentence = parts[1].strip()
    msg = await message.answer("⏳ Checking grammar with AI...")

    # Try backend first, fallback to direct OpenRouter
    api_key = OPENROUTER_API_KEY or os.getenv("OPENROUTER_API_KEY")
    if api_key:
        try:
            prompt = f"""You are an expert IELTS English teacher for Uzbek students.

Analyze this English sentence and return ONLY valid JSON:

Sentence: "{user_sentence}"

Return this exact JSON structure:
{{
  "is_correct": true or false,
  "corrected_answer": "corrected version",
  "better_version": "improved version at B2 level",
  "explanation_uz": "Uzbek explanation of the error and how to fix it",
  "used_grammar": ["grammar structures used"],
  "score": 8
}}"""

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{OPENROUTER_BASE_URL}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": OPENROUTER_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.3,
                        "max_tokens": 800,
                    },
                )

            if resp.status_code == 200:
                content = resp.json()["choices"][0]["message"]["content"]
                text = content.strip()
                if text.startswith("```"):
                    text = text.split("\n", 1)[1]
                if text.endswith("```"):
                    text = text.rsplit("```", 1)[0]
                result = json.loads(text.strip())

                score = result.get("score", 0)
                is_correct = result.get("is_correct", False)
                icon = "✅" if is_correct else "❌"
                status_text = "Correct!" if is_correct else "Needs Improvement"

                reply = (
                    f"{icon} <b>Grammar Check Result</b>\n\n"
                    f"<b>Your sentence:</b>\n{user_sentence}\n\n"
                    f"<b>Status:</b> {status_text}\n"
                    f"<b>Score:</b> {score}/10\n\n"
                )
                if result.get("corrected_answer"):
                    reply += f"<b>Corrected:</b>\n{result['corrected_answer']}\n\n"
                if result.get("better_version"):
                    reply += f"<b>Better version:</b>\n{result['better_version']}\n\n"
                if result.get("explanation_uz"):
                    reply += f"<b>Explanation (UZ):</b>\n{result['explanation_uz']}\n"

                await msg.edit_text(reply, parse_mode=ParseMode.HTML)
                return
        except Exception:
            pass

    # Fallback
    await msg.edit_text(
        f"<b>Your sentence:</b>\n{user_sentence}\n\n"
        f"⚠️ AI check requires OpenRouter key in .env or backend running.\n"
        f"Set OPENROUTER_API_KEY or start the backend server.",
        parse_mode=ParseMode.HTML,
    )


def _format_grammar_pattern(pattern, section_title=None):
    lines = []
    if section_title:
        lines.append(f"<b>📚 {section_title}</b>\n")
    lines.append(f"<b>{pattern['title_en']}</b>")
    lines.append(f"🇺🇿 {pattern['title_uz']}")
    lines.append("")
    lines.append(f"<b>Level:</b> {pattern['level']}")
    lines.append(f"<b>Category:</b> {pattern['category']}")
    if pattern.get("ielts_part"):
        lines.append(f"<b>IELTS Part:</b> {pattern['ielts_part']}")
    lines.append("")
    lines.append(f"<b>Formula:</b>")
    lines.append(f"<code>{pattern['formula']}</code>")
    lines.append("")
    lines.append(f"<b>{pattern['meaning_uz']}</b>")
    lines.append("")
    lines.append(f"{pattern['explanation_uz'][:400]}")
    if len(pattern.get("when_to_use_uz", "")) > 0:
        lines.append("")
        lines.append(f"<b>When to use:</b>")
        lines.append(f"{pattern['when_to_use_uz'][:300]}")

    # Add examples if available
    conn = get_db()
    examples = conn.execute(
        "SELECT example_en, example_uz FROM grammar_examples WHERE pattern_id = ? LIMIT 2",
        (pattern["id"],),
    ).fetchall()
    conn.close()

    if examples:
        lines.append("")
        lines.append("<b>Examples:</b>")
        for ex in examples:
            lines.append(f"EN: {ex['example_en']}")
            lines.append(f"UZ: {ex['example_uz']}")
            lines.append("")

    return "\n".join(lines)


@dp.message(F.text == "📚 Grammar")
async def cmd_grammar_btn(message: Message):
    await cmd_grammar(message)


@dp.message(F.text == "ℹ️ Yordam")
async def cmd_help_btn(message: Message):
    await cmd_help(message)


@dp.callback_query(F.data.startswith("tp:"))
async def cb_topic(callback: CallbackQuery):
    topic = callback.data.split(":")[1]
    conn = get_db()
    total = conn.execute(
        "SELECT COUNT(*) FROM vocab_enriched WHERE topic = ?", (topic,)
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT * FROM vocab_enriched WHERE topic = ? ORDER BY RANDOM() LIMIT 8",
        (topic,),
    ).fetchall()
    conn.close()

    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    if not rows:
        await callback.message.answer(f"❌ '<code>{topic}</code>' mavzusida so'zlar topilmadi.")
        return

    await callback.message.answer(f"📂 <b>{topic}</b> — {total} so'z, {len(rows)} tasodifiy:")
    for i, r in enumerate(rows, 1):
        await callback.message.answer(
            format_vocab(r, i),
            reply_markup=gen_keyboard(r['id']),
        )


@dp.callback_query(F.data.startswith("gen:"))
async def cb_gen(callback: CallbackQuery):
    vocab_id = int(callback.data.split(":")[1])
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM vocab_enriched WHERE id = ?", (vocab_id,)
    ).fetchone()
    conn.close()

    if not row:
        await callback.answer("❌ So'z topilmadi.", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    kb = mode_keyboard(vocab_id)
    await callback.message.answer(
        f"🤖 <b>{row['english']}</b> uchun generation rejimini tanlang:",
        reply_markup=kb,
    )


@dp.callback_query(F.data.startswith("mode:"))
async def cb_mode(callback: CallbackQuery):
    parts = callback.data.split(":")
    vocab_id = int(parts[1])
    mode = parts[2]

    conn = get_db()
    row = conn.execute(
        "SELECT * FROM vocab_enriched WHERE id = ?", (vocab_id,)
    ).fetchone()
    conn.close()

    if not row:
        await callback.answer("❌ So'z topilmadi.", show_alert=True)
        return

    await callback.answer("⏳ AI jumlani yaratmoqda...")
    await callback.message.edit_reply_markup(reply_markup=None)

    result = await generate_ai(
        row["english"], row["uzbek"], row["topic"], row["type"], row["level"], mode
    )

    if not result:
        await callback.message.answer(
            "❌ AI generatsiya amalga oshirilmadi.\n"
            "Qayta urinib ko'ring: /gen " + row["english"]
        )
        return

    text = (
        f"<b>🤖 AI Generated Sentence</b>\n\n"
        f"<b>🇬🇧 English:</b>\n{result.get('sentence_en', '')}\n\n"
        f"<b>🇺🇿 Uzbek:</b>\n{result.get('sentence_uz', '')}\n\n"
        f"<b>💡 Tushuntirish:</b>\n{result.get('explanation_uz', '')}\n\n"
        f"📝 Mode: {result.get('mode', '')}"
    )
    await callback.message.answer(text)


@dp.message()
async def handle_text(message: Message):
    text = message.text.strip()

    conn = get_db()
    pattern = f"%{text}%"
    rows = conn.execute(
        """SELECT * FROM vocab_enriched
           WHERE english LIKE ? OR uzbek LIKE ? OR definition LIKE ?
           ORDER BY CASE WHEN english LIKE ? THEN 0 WHEN uzbek LIKE ? THEN 1 ELSE 2 END
           LIMIT 3""",
        (pattern, pattern, pattern, text, text),
    ).fetchall()
    conn.close()

    if rows:
        await message.answer(f"🔍 '<b>{text}</b>' bo'yicha topilganlar:")
        for i, r in enumerate(rows, 1):
            await message.answer(
                format_vocab(r, i),
                reply_markup=gen_keyboard(r['id']),
            )
    else:
        await message.answer(
            "🤔 Noto'g'ru buyruq yoki so'z.\n"
            "Yordam uchun /help yoki tugmalardan foydalaning.",
            reply_markup=main_keyboard(),
        )


async def main():
    print("🤖 Bot is starting...")
    await dp.start_polling(bot, handle_signals=False)


if __name__ == "__main__":
    asyncio.run(main())
