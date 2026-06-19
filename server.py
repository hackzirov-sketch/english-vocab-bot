import os
import sys
import threading
from app import app
from app import run_bot_background

if __name__ == "__main__":
    t = threading.Thread(target=run_bot_background, daemon=True)
    t.start()
    import time
    time.sleep(2)

    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
