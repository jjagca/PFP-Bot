# Twitter Replicate Bot (Polling)

A minimal polling bot for X (Twitter) that:
- Listens for mentions of your bot handle.
- Processes tweets with photos OR uses profile pictures as image sources.
- Sends the person image + your two static assets (sunglasses, gradient) to **google/nano-banana** on Replicate.
- Replies to the tweet with the edited PNG.
- Avoids duplicate processing by tracking liked tweets.

## Quick start (local)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your keys & URLs
python main.py
```

The bot will poll every `POLL_SECONDS` seconds (default 25). It saves the last seen tweet ID to `.last_id`.

## Environment variables

See `.env.example`. Required:

- `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`, `X_BEARER_TOKEN`
- `REPLICATE_API_TOKEN`
- `MODEL_REF` (default `google/nano-banana`)
- `NANO_PROMPT` (your fixed instruction)
- `SUNGLASSES_URL` and `BACKGROUND_URL` (public URLs, e.g., GitHub RAW)
- `BOT_HANDLE` (without @)
- `POLL_SECONDS` (integer, e.g., 25 or 30)

Optional:
- `SKIP_IF_LIKED` (default `1`) - Skip tweets the bot has already liked
- `LIKED_PRELOAD_LIMIT` (default `500`) - Number of recent liked tweets to preload

## Render (free tier) deploy

1. Push this repo to GitHub.
2. On **render.com** → **New** → **Web Service** → pick your repo.
3. **Environment:** Python 3.11
4. **Build Command:** `pip install --no-cache-dir -r requirements.txt`
5. **Start Command:** `python main.py`
6. Add all env vars from `.env.example` in the Render dashboard.
7. Deploy and watch logs. You should see: `🚀 bot up. last_id=None`.

> Free tier may sleep/ pause. For always-on, switch the instance to Starter.

## Notes
- Bot processes tweets with attached photos OR uses profile pictures when no photo is attached.
- Image source priority: 1) Attached photo, 2) Mentioned user's avatar, 3) Author's avatar.
- Uses three inputs for nano-banana: `[person_image_url, SUNGLASSES_URL, BACKGROUND_URL]`.
- Replies with a PNG via v1.1 media upload.
- Avoids duplicate processing by liking processed tweets (Twitter-as-state).
- Do **not** commit `.env` or real secrets. Keep `.env` out of git.

## License
MIT
