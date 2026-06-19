# English Vocabulary Master - Telegram Bot

Standalone Telegram bot for learning English vocabulary. Works with direct SQLite database access (no backend required).

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Create `.env` from `.env.example` and add your BOT_TOKEN.

## Run

```bash
run_bot.bat
```

Or manually:

```bash
python bot.py
```

## Commands

- `/start` - Introduction
- `/topics` - All topics
- `/search word` - Search vocabulary
- `/quiz` - Random quiz
- `/gen word` - AI sentence generation
- `/grammar` - Grammar sections
- `/grammar_quiz` - Grammar quiz
- `/check_grammar sentence` - AI grammar check
- `/help` - Help

## Notes

- BOT_TOKEN is required in `.env` for the bot to work.
- The bot works standalone with just the database in `database/` folder.
- For AI features, GROQ_API_KEY or OPENROUTER_API_KEY must be set.
