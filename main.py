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

# Replicate
os.environ["REPLICATE_API_TOKEN"] = os.getenv("REPLICATE_API_TOKEN")

def load_last_id():
    try:
        with open(LAST_ID_FILE, "r") as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def save_last_id(tid: str):
    with open(LAST_ID_FILE, "w") as f:
        f.write(tid)


def fetch_mentions(since_id=None):
    query = f"@{BOT_HANDLE} -is:retweet"
    return client.search_recent_tweets(
        query=query,
        since_id=since_id,
        tweet_fields="id,author_id,attachments,created_at",
        expansions="author_id,attachments.media_keys",
        media_fields="url,type",
        user_fields="username",
        max_results=50,
    )


def username_map_from_includes(includes):
    users = getattr(includes, "users", []) if includes else []
    return {str(u.id): u.username for u in users}


def media_map_from_includes(includes):
    media = getattr(includes, "media", []) if includes else []
    return {m.media_key: m for m in media}


def first_photo_url(tweet, media_map):
    keys = (tweet.attachments.media_keys if tweet.attachments else []) or []
    for k in keys:
        m = media_map.get(k)
        if m and m.type == "photo" and getattr(m, "url", None):
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
    """
    Calls google/nano-banana with three inputs and returns local PNG path.
    """
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
    client.create_tweet(
        text=f"@{username}",
        in_reply_to_tweet_id=in_reply_to_tweet_id,
        media={"media_ids": [media_id]},
    )


def process_tweet(tweet, usernames, media_map):
    # Only proceed if tweet has a photo; otherwise ignore
    person_url = first_photo_url(tweet, media_map)
    if not person_url:
        print(f"⏭️  {tweet.id}: no photo attached; skipping.")
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
    finally:
        try:
            os.remove(out_path)
        except Exception:
            pass


def main():
    last_id = load_last_id()
    print(f"🚀 bot up. last_id={last_id}")
    while True:
        try:
            resp = fetch_mentions(last_id)
            if resp.data:
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
