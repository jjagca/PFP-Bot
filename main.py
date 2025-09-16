import os
import time
import tempfile
import tweepy
import requests
import replicate
from dotenv import load_dotenv

load_dotenv()

# --- Config
BOT_HANDLE     = os.getenv("BOT_HANDLE")
POLL_SECONDS   = int(os.getenv("POLL_SECONDS", "25"))
MODEL_REF      = os.getenv("MODEL_REF", "google/nano-banana")
NANO_PROMPT    = os.getenv("NANO_PROMPT", "")
SUNGLASSES_URL = os.getenv("SUNGLASSES_URL")
BACKGROUND_URL = os.getenv("BACKGROUND_URL")
LAST_ID_FILE   = ".last_id"

# Twitter-as-state config
SKIP_IF_LIKED       = os.getenv("SKIP_IF_LIKED", "1").lower() not in ("0", "false", "no")
LIKED_PRELOAD_LIMIT = int(os.getenv("LIKED_PRELOAD_LIMIT", "500"))

if not BOT_HANDLE:
    raise RuntimeError("BOT_HANDLE is required (without the @).")

# --- Auth
client = tweepy.Client(
    bearer_token=os.getenv("X_BEARER_TOKEN"),
    consumer_key=os.getenv("X_API_KEY"),
    consumer_secret=os.getenv("X_API_SECRET"),
    access_token=os.getenv("X_ACCESS_TOKEN"),
    access_token_secret=os.getenv("X_ACCESS_TOKEN_SECRET"),
    wait_on_rate_limit=True,
)
# v1.1 for media upload
auth = tweepy.OAuth1UserHandler(
    os.getenv("X_API_KEY"),
    os.getenv("X_API_SECRET"),
    os.getenv("X_ACCESS_TOKEN"),
    os.getenv("X_ACCESS_TOKEN_SECRET"),
)
api_v1 = tweepy.API(auth, wait_on_rate_limit=True)

print("Tweepy version:", tweepy.__version__)

# Replicate
os.environ["REPLICATE_API_TOKEN"] = os.getenv("REPLICATE_API_TOKEN")

# In-memory cache of liked tweet IDs for the authenticated bot
MY_USER_ID = None
LIKED_IDS = set()


def load_last_id():
    try:
        with open(LAST_ID_FILE, "r") as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def save_last_id(tid) -> None:
    with open(LAST_ID_FILE, "w") as f:
        f.write(str(tid))


def fetch_mentions(since_id=None):
    query = f"@{BOT_HANDLE} -is:retweet"
    return client.search_recent_tweets(
        query=query,
        since_id=since_id,
        tweet_fields="id,author_id,attachments,created_at",
        expansions="author_id,attachments.media_keys",
        media_fields="url,type,preview_image_url",
        user_fields="username",
        max_results=50,
    )

# ---- Helpers to normalize Tweepy response structures (object vs dict style) ----

def _get_includes_collection(includes, key):
    if not includes:
        return []
    if isinstance(includes, dict):
        return includes.get(key, []) or []
    return getattr(includes, key, []) or []


def username_map_from_includes(includes):
    users = _get_includes_collection(includes, "users")
    return {str(u.id): u.username for u in users}


def media_map_from_includes(includes):
    media_items = _get_includes_collection(includes, "media")
    result = {}
    for m in media_items:
        mk = getattr(m, "media_key", None)
        if mk:
            result[mk] = m
    return result


def _extract_media_keys(attachments):
    if not attachments:
        return []
    if isinstance(attachments, dict):
        return attachments.get("media_keys", []) or []
    return getattr(attachments, "media_keys", []) or []


def first_photo_url(tweet, media_map):
    keys = _extract_media_keys(getattr(tweet, "attachments", None))
    for k in keys:
        m = media_map.get(k)
        if not m:
            continue
        if getattr(m, "type", None) == "photo" and getattr(m, "url", None):
            return m.url
    return None


def write_bytes_tmp(content: bytes, suffix=".png") -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(content)
    return path


def download_tmp(url: str, suffix=".png") -> str:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return write_bytes_tmp(r.content, suffix)


def run_nano_banana(person_url: str, sunglasses_url: str, background_url: str, prompt: str) -> str:
    """Calls google/nano-banana with three inputs and returns local PNG path."""
    out = replicate.run(
        MODEL_REF,
        input={
            "prompt": prompt,
            "image_input": [person_url, sunglasses_url, background_url],
            "output_format": "png",
        },
    )
    # Try file-like first
    try:
        data = out.read()
        return write_bytes_tmp(data, ".png")
    except Exception:
        pass
    # List of URLs
    if isinstance(out, list) and out:
        return download_tmp(str(out[0]), ".png")
    # Single URL
    if isinstance(out, str):
        return download_tmp(out, ".png")
    # Object with .url()
    try:
        return download_tmp(out.url(), ".png")
    except Exception:
        raise RuntimeError(f"Unexpected Replicate output type: {type(out)}")


def upload_media(path: str) -> str:
    media = api_v1.media_upload(filename=path)
    return str(media.media_id)


def reply_with_media(in_reply_to_tweet_id: str, media_id: str, username: str):
    """
    Try multiple Tweepy signatures for compatibility.
    1. Older v2 style: media_ids=[...] (no 'media' dict)
    2. Newer style (if available): reply={}, media={}
    3. v1.1 fallback via api_v1.update_status
    """
    text = f"@{username}"

    # Attempt 1: flattened media_ids + in_reply_to_tweet_id
    try:
        client.create_tweet(
            text=text,
            in_reply_to_tweet_id=in_reply_to_tweet_id,
            media_ids=[media_id],
        )
        return
    except TypeError:
        pass

    # Attempt 2: nested dict style
    try:
        client.create_tweet(
            text=text,
            reply={"in_reply_to_tweet_id": in_reply_to_tweet_id},
            media={"media_ids": [media_id]},
        )
        return
    except TypeError:
        pass

    # Attempt 3: v1.1 fallback
    try:
        api_v1.update_status(
            status=text,
            in_reply_to_status_id=in_reply_to_tweet_id,
            auto_populate_reply_metadata=True,
            media_ids=[media_id],
        )
        return
    except Exception as e:
        print("⚠️ failed to send reply via all methods:", e)


# ---------- Twitter-as-state helpers (likes) ----------

def _get_next_token(meta):
    # Works whether Tweepy returns dict-like or object-like meta
    try:
        # dict-like
        return meta.get("next_token")
    except Exception:
        pass
    try:
        # object-like
        return getattr(meta, "next_token", None)
    except Exception:
        return None


def resolve_my_user_id():
    me = client.get_me()
    if not me or not me.data:
        raise RuntimeError("Unable to resolve authenticated user via client.get_me()")
    return str(me.data.id)


def preload_liked_ids(user_id: str, limit: int = 500) -> set:
    liked = set()
    pagination_token = None
    while len(liked) < limit:
        resp = client.get_liked_tweets(
            id=user_id,
            max_results=100,
            pagination_token=pagination_token,
            tweet_fields="id",
        )
        if not resp or not resp.data:
            break
        for t in resp.data:
            liked.add(str(t.id))
            if len(liked) >= limit:
                break
        meta = getattr(resp, "meta", {}) or {}
        pagination_token = _get_next_token(meta)
        if not pagination_token:
            break
    print(f"💾 preloaded {len(liked)} liked tweet IDs (limit={limit})")
    return liked


def like_tweet(tweet_id: str):
    try:
        client.like(tweet_id)
        return True
    except Exception as e:
        print(f"⚠️ failed to like tweet {tweet_id}: {e}")
        return False


def process_tweet(tweet, usernames, media_map):
    # Skip if already liked by the bot (Twitter-as-state)
    if SKIP_IF_LIKED and str(tweet.id) in LIKED_IDS:
        print(f"⏭️  {tweet.id}: already liked; skipping.")
        return

    person_url = first_photo_url(tweet, media_map)
    if not person_url:
        has_attachments = bool(getattr(tweet, "attachments", None))
        print(f"⏭️  {tweet.id}: no usable photo (attachments={has_attachments}); skipping.")
        return
    if not SUNGLASSES_URL or not BACKGROUND_URL:
        print("❗ Set SUNGLASSES_URL and BACKGROUND_URL in your environment")
        return

    out_path = run_nano_banana(person_url, SUNGLASSES_URL, BACKGROUND_URL, NANO_PROMPT)
    try:
        media_id = upload_media(out_path)
        handle = usernames.get(str(tweet.author_id), "")
        reply_with_media(tweet.id, media_id, handle)
        print(f"✅ Replied to {tweet.id} (@{handle})")

        # Like after successful reply and add to cache
        if SKIP_IF_LIKED:
            if like_tweet(tweet.id):
                LIKED_IDS.add(str(tweet.id))
                print(f"⭐ Liked mention {tweet.id} to mark as processed.")
    finally:
        try:
            os.remove(out_path)
        except Exception:
            pass


def main():
    global MY_USER_ID, LIKED_IDS

    # Resolve bot user and preload likes once on startup
    try:
        MY_USER_ID = resolve_my_user_id()
        print(f"👤 Authenticated as user_id={MY_USER_ID}")
        if SKIP_IF_LIKED:
            LIKED_IDS = preload_liked_ids(MY_USER_ID, LIKED_PRELOAD_LIMIT)
    except Exception as e:
        print(f"⚠️ could not initialize likes state: {e}")
        # Continue without likes state if desired

    last_id = load_last_id()
    print(f"🚀 bot up. last_id={last_id} skip_if_liked={SKIP_IF_LIKED}")

    while True:
        try:
            resp = fetch_mentions(last_id)
            if resp and resp.data:
                tweets = sorted(resp.data, key=lambda t: int(t.id))
                usernames = username_map_from_includes(resp.includes)
                media_map = media_map_from_includes(resp.includes)

                for t in tweets:
                    process_tweet(t, usernames, media_map)

                last_id = tweets[-1].id
                save_last_id(last_id)
        except Exception as e:
            print("⚠️ error:", e)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()