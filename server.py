import threading
from fastapi import FastAPI

# Start the polling bot in a background thread

def _start_bot():
    # Import inside the function to avoid side effects at import time
    from main import main as run_poller
    run_poller()

app = FastAPI()

@app.get("/")
def root():
    return {"status": "ok"}

@app.get("/healthz")
def healthz():
    return {"ok": True}

# Launch the bot loop once at startup
threading.Thread(target=_start_bot, daemon=True).start()