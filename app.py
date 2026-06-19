import os
import asyncio
import threading
from flask import Flask, jsonify

app = Flask(__name__)

bot_running = False


def run_bot_background():
    global bot_running
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    import bot
    loop.run_until_complete(bot.main())


@app.route("/")
def home():
    return jsonify({
        "status": "running",
        "bot": bot_running,
    })


@app.route("/health")
def health():
    if bot_running:
        return jsonify({"status": "ok"}), 200
    return jsonify({"status": "starting"}), 503


if __name__ == "__main__":
    t = threading.Thread(target=run_bot_background, daemon=True)
    t.start()
    bot_running = True

    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
