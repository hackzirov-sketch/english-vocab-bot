import os
import sys
import platform
import asyncio
import threading
import traceback
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template_string, jsonify, request

app = Flask(__name__)

bot_running = False
bot_start_time = None
bot_error = None


def run_bot_background():
    global bot_running, bot_start_time, bot_error
    bot_start_time = datetime.now()
    bot_error = None
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    import bot as bot_module

    async def start():
        global bot_running
        await bot_module.bot.delete_webhook(drop_pending_updates=True)
        print("Webhook deleted, starting polling...")
        bot_running = True
        await bot_module.main()

    try:
        loop.run_until_complete(start())
    except Exception as e:
        bot_error = traceback.format_exc()
        print(f"Bot error: {e}")
        print(bot_error)
    finally:
        bot_running = False


INDEX_HTML = """
<!DOCTYPE html>
<html lang="uz">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>English Vocabulary Bot</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, sans-serif;
            background: linear-gradient(135deg, #0f0c29, #302b63, #24243e);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #fff;
        }
        .card {
            background: rgba(255,255,255,0.05);
            backdrop-filter: blur(20px);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 24px;
            padding: 48px 40px;
            max-width: 520px;
            width: 90%;
            text-align: center;
            box-shadow: 0 25px 60px rgba(0,0,0,0.5);
        }
        .icon { font-size: 64px; margin-bottom: 16px; }
        h1 {
            font-size: 28px;
            font-weight: 700;
            margin-bottom: 8px;
            background: linear-gradient(to right, #a78bfa, #60a5fa);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .subtitle {
            color: rgba(255,255,255,0.6);
            font-size: 14px;
            margin-bottom: 32px;
        }
        .status {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 8px 20px;
            border-radius: 100px;
            font-size: 14px;
            font-weight: 500;
            margin-bottom: 32px;
        }
        .status.ok {
            background: rgba(74,222,128,0.15);
            color: #4ade80;
        }
        .status.err {
            background: rgba(248,113,113,0.15);
            color: #f87171;
        }
        .status .dot {
            width: 8px; height: 8px;
            border-radius: 50%;
        }
        .status.ok .dot { background: #4ade80; animation: pulse 1.5s infinite; }
        .status.err .dot { background: #f87171; }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.3; }
        }
        .info {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
            text-align: left;
            margin-bottom: 32px;
        }
        .info-item {
            background: rgba(255,255,255,0.05);
            border-radius: 12px;
            padding: 12px 16px;
        }
        .info-item .label {
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: rgba(255,255,255,0.4);
            margin-bottom: 4px;
        }
        .info-item .value {
            font-size: 14px;
            font-weight: 600;
            color: rgba(255,255,255,0.9);
        }
        .btn {
            display: inline-block;
            background: linear-gradient(to right, #7c3aed, #3b82f6);
            color: #fff;
            text-decoration: none;
            padding: 12px 32px;
            border-radius: 100px;
            font-weight: 600;
            font-size: 15px;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 30px rgba(124,58,237,0.4);
        }
        .footer {
            margin-top: 24px;
            font-size: 12px;
            color: rgba(255,255,255,0.3);
        }
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">📚</div>
        <h1>English Vocabulary Bot</h1>
        <p class="subtitle">3480+ so'z, 24 mavzu, AI yordamida o'rganing</p>
        <div class="status {{ 'ok' if bot_running else 'err' }}">
            <span class="dot"></span>
            {{ 'Bot ishlayapti' if bot_running else 'Bot ishlamayapti' }}
        </div>
        <div class="info">
            <div class="info-item">
                <div class="label">Platforma</div>
                <div class="value">{{ platform }}</div>
            </div>
            <div class="info-item">
                <div class="label">Python</div>
                <div class="value">{{ python_version }}</div>
            </div>
            <div class="info-item">
                <div class="label">Ish vaqti</div>
                <div class="value">{{ uptime }}</div>
            </div>
            <div class="info-item">
                <div class="label">Ma'lumotlar</div>
                <div class="value">{{ db_size }}</div>
            </div>
        </div>
        <a class="btn" href="https://t.me/{{ bot_username }}" target="_blank">Botni ochish</a>
        <div class="footer">Powered by Flask + aiogram + Render</div>
    </div>
</body>
</html>
"""


@app.route("/")
def home():
    uptime = "0 min"
    if bot_start_time:
        diff = datetime.now() - bot_start_time
        hours, remainder = divmod(int(diff.total_seconds()), 3600)
        minutes = remainder // 60
        uptime = f"{hours} soat {minutes} min" if hours else f"{minutes} min"

    db_path = Path(__file__).resolve().parent / "database" / "master_maximal_v14_openrouter_ready.db"
    db_size = "Noma'lum"
    if db_path.exists():
        size_mb = db_path.stat().st_size / (1024 * 1024)
        db_size = f"{size_mb:.1f} MB"

    return render_template_string(
        INDEX_HTML,
        bot_running=bot_running,
        platform=platform.system(),
        python_version=platform.python_version(),
        uptime=uptime,
        db_size=db_size,
        bot_username="englishVinglishUzbekish_bot",
    )


@app.route("/health")
def health():
    if bot_running:
        return jsonify({"status": "ok"}), 200
    return jsonify({"status": "starting", "error": bot_error}), 503


@app.route("/debug")
def debug():
    admin_key = os.getenv("ADMIN_KEY")
    if admin_key and request.args.get("key") != admin_key:
        return jsonify({"error": "Unauthorized"}), 403
    info = {
        "bot_running": bot_running,
        "python": platform.python_version(),
        "platform": platform.system(),
        "has_bot_token": bool(os.getenv("BOT_TOKEN")),
        "has_openrouter": bool(os.getenv("OPENROUTER_API_KEY")),
        "db_exists": Path(__file__).resolve().parent.joinpath("database", "master_maximal_v14_openrouter_ready.db").exists(),
        "error": bot_error,
    }
    return jsonify(info)


if __name__ == "__main__":
    import time
    t = threading.Thread(target=run_bot_background, daemon=True)
    t.start()
    time.sleep(2)
    print(f"Bot running: {bot_running}")
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
