# Twitter Replicate Bot (Polling)

A minimal polling bot for X (Twitter) that:
- Listens for mentions of your bot handle.
- Processes tweets with photos OR uses profile pictures as image sources.
- Sends the person image + your two static assets (sunglasses, gradient) to **google/nano-banana** on Replicate.
- Replies to the tweet with the edited PNG.
- Avoids duplicate processing with local state tracking and optional tweet liking.
- Includes comprehensive hardening features to reduce automation detection signals.

## Quick start (local)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your keys & URLs
python main.py
```

The bot will poll every `POLL_SECONDS` seconds (default 25) with optional jitter. It saves the last seen tweet ID to `.last_id` and tracks processed tweets in `.processed_ids`.

## Environment variables

See `.env.example`. Required:

- `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`, `X_BEARER_TOKEN`
- `REPLICATE_API_TOKEN`
- `MODEL_REF` (default `google/nano-banana`)
- `NANO_PROMPT` (your fixed instruction)
- `SUNGLASSES_URL` and `BACKGROUND_URL` (public URLs, e.g., GitHub RAW)
- `BOT_HANDLE` (without @)
- `POLL_SECONDS` (integer, e.g., 25 or 30)

Optional (backward compatible):
- `SKIP_IF_LIKED` (default `1`) - Skip tweets the bot has already liked
- `LIKED_PRELOAD_LIMIT` (default `500`) - Number of recent liked tweets to preload

### New Hardening & Authenticity Features

These features help reduce spam/automation signals while maintaining backward compatibility:

#### Reply Behavior
- **`LIKE_MODE`** (default: `all`) - Liking strategy: `all`, `probabilistic`, or `none`
  - `all`: Like every processed tweet (backward compatible)
  - `probabilistic`: Like based on `LIKE_PROB` probability
  - `none`: Don't like any tweets
- **`LIKE_PROB`** (default: `0.7`) - Probability of liking when `LIKE_MODE=probabilistic` (0.0-1.0)

#### Humanization
- **`HUMANIZE_DELAY`** (default: `1`) - Enable human-like delays before replies (1=enabled, 0=disabled)
- **`REPLY_MIN_DELAY`** (default: `2`) - Minimum delay in seconds before replying
- **`REPLY_MAX_DELAY`** (default: `8`) - Maximum delay in seconds before replying
- **`POLL_JITTER_MAX`** (default: `5`) - Maximum random jitter added to poll interval in seconds

#### Rate Limiting
- **`PER_USER_MAX`** (default: `0`) - Maximum replies per user per day (0=unlimited)
- **`GLOBAL_MAX`** (default: `0`) - Maximum total replies per day (0=unlimited)

#### Media & Content
- **`ALT_TEXT`** (default: `1`) - Add descriptive alt text to uploaded media (1=enabled, 0=disabled)
- **`VARIANT_ENABLE`** (default: `0`) - Apply subtle image variation to reduce perceptual hash clustering (requires Pillow, optional)
- **`PROMPT_UNIQUIFIER`** (default: `1`) - Add session token to prompts to mitigate cache/dedup (1=enabled, 0=disabled)

#### State Management
- **`PROCESSED_STATE_FILE`** (default: `.processed_ids`) - Local file for tracking processed tweet IDs
- **`PROCESSED_STATE_CAP`** (default: `10000`) - Maximum number of processed IDs to keep in state file

## Features

### Core Functionality
- **Smart image source priority**: 1) Attached photo, 2) Right-most mentioned user's avatar (excluding bot and author), 3) Author's avatar
- **Self-recursion prevention**: Automatically skips tweets authored by the bot
- **Query optimization**: Excludes bot's own tweets and retweets from search query

### Hardening & Anti-Spam
- **Reply text diversification**: 5 different reply templates chosen randomly
- **Human-like timing**: Random delays before replies and jitter in poll intervals
- **Rate limiting**: Per-user and global daily caps with automatic daily reset
- **Flexible liking**: Decouple likes from processing with probabilistic or disabled liking
- **Local state persistence**: Maintains processed tweet IDs independently of likes
- **Prompt uniquification**: Adds session-specific tokens to avoid prompt caching
- **Subtle image variation**: Optional micro-adjustments to reduce perceptual hash clustering (requires Pillow)
- **Alt text support**: Adds descriptive alt text for accessibility and authenticity

### Safety & Reliability
- **Safer dedupe order**: Checks local processed set → liked set → rate limits → generation
- **Graceful fallbacks**: Pillow usage is optional; features degrade gracefully when unavailable
- **Comprehensive logging**: Clear markers for all skip conditions and processing stages
- **Backward compatibility**: All new features are opt-in or have safe defaults

## Render (free tier) deploy

1. Push this repo to GitHub.
2. On **render.com** → **New** → **Web Service** → pick your repo.
3. **Environment:** Python 3.11
4. **Build Command:** `pip install --no-cache-dir -r requirements.txt`
5. **Start Command:** `python main.py`
6. Add all env vars from `.env.example` in the Render dashboard.
7. Deploy and watch logs. You should see: `🚀 bot up. last_id=None`.

> Free tier may sleep/pause. For always-on, switch the instance to Starter.

## Notes
- Bot processes tweets with attached photos OR uses profile pictures when no photo is attached.
- Uses three inputs for nano-banana: `[person_image_url, SUNGLASSES_URL, BACKGROUND_URL]`.
- Replies with a PNG via v1.1 media upload.
- Avoids duplicate processing via local state file (`.processed_ids`) and optional tweet liking.
- Do **not** commit `.env` or real secrets. Keep `.env` out of git.
- The `.processed_ids` and `.last_id` files are automatically excluded from git.

## Optional Dependencies

- **Pillow**: Required only if `VARIANT_ENABLE=1`. Install with `pip install Pillow` if you want to enable image variation.
- **numpy**: Required with Pillow for image variation. Install with `pip install numpy` if needed.

## Testing

The bot includes built-in validation for:
- Configuration loading and defaults
- Local state persistence with cap enforcement
- Rate limiting (per-user and global)
- Reply text variation
- Prompt uniquification
- Image variation (with graceful fallback)

Run basic validation:
```bash
python -c "import main; print('✅ Configuration loaded successfully')"
```

## License
MIT
