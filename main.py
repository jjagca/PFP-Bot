import os
import time
import tempfile
import tweepy
import requests
import replicate
import random
import string
from datetime import datetime, timedelta
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

# --- Config
BOT_HANDLE           = os.getenv("BOT_HANDLE")
POLL_SECONDS         = int(os.getenv("POLL_SECONDS", "25"))
MODEL_REF            = os.getenv("MODEL_REF", "google/nano-banana")
NANO_PROMPT          = os.getenv("NANO_PROMPT", "")
SUNGLASSES_URL       = os.getenv("SUNGLASSES_URL")
BACKGROUND_URL       = os.getenv("BACKGROUND_URL")
SKIP_IF_LIKED        = os.getenv("SKIP_IF_LIKED", "1") == "1"
LIKED_PRELOAD_LIMIT  = int(os.getenv("LIKED_PRELOAD_LIMIT", "500"))
LAST_ID_FILE         = ".last_id"

# New hardening & authenticity config
LIKE_MODE            = os.getenv("LIKE_MODE", "all")  # all|probabilistic|none
LIKE_PROB            = float(os.getenv("LIKE_PROB", "0.7"))  # for probabilistic mode
HUMANIZE_DELAY       = os.getenv("HUMANIZE_DELAY", "1") == "1"
REPLY_MIN_DELAY      = int(os.getenv("REPLY_MIN_DELAY", "2"))
REPLY_MAX_DELAY      = int(os.getenv("REPLY_MAX_DELAY", "8"))
POLL_JITTER_MAX      = int(os.getenv("POLL_JITTER_MAX", "5"))
PER_USER_MAX         = int(os.getenv("PER_USER_MAX", "0"))  # 0=unlimited
GLOBAL_MAX           = int(os.getenv("GLOBAL_MAX", "0"))  # 0=unlimited
ALT_TEXT             = os.getenv("ALT_TEXT", "1") == "1"
VARIANT_ENABLE       = os.getenv("VARIANT_ENABLE", "0") == "1"
PROMPT_UNIQUIFIER    = os.getenv("PROMPT_UNIQUIFIER", "1") == "1"
PROCESSED_STATE_FILE = os.getenv("PROCESSED_STATE_FILE", ".processed_ids")
PROCESSED_STATE_CAP  = int(os.getenv("PROCESSED_STATE_CAP", "10000"))

# Reply text variants for diversification
REPLY_VARIANTS = [
    "@{username}",
    "@{username} 😎",
    "@{username} ✨",
    "@{username} Here you go!",
    "@{username} Looking good!",
]

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

# Global state for likes-as-state and user caching
liked_tweet_ids = set()
user_profile_cache = {}  # username -> profile_image_url
bot_user_id = None

# Local processed state (independent of likes)
processed_tweet_ids = set()

# Rate limiting state (resets daily)
user_reply_counts = defaultdict(int)  # username -> count
global_reply_count = 0
rate_limit_reset_date = datetime.now().date()

# Session token for prompt uniquification
session_token = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))

# Startup logging
print(f"🚀 Bot Configuration:")
print(f"   LIKE_MODE: {LIKE_MODE}")
if LIKE_MODE == "probabilistic":
    print(f"   LIKE_PROB: {LIKE_PROB}")
print(f"   HUMANIZE_DELAY: {HUMANIZE_DELAY}")
if HUMANIZE_DELAY:
    print(f"   REPLY_DELAY: {REPLY_MIN_DELAY}-{REPLY_MAX_DELAY}s")
print(f"   POLL_JITTER_MAX: {POLL_JITTER_MAX}s")
if PER_USER_MAX > 0:
    print(f"   PER_USER_MAX: {PER_USER_MAX}/day")
if GLOBAL_MAX > 0:
    print(f"   GLOBAL_MAX: {GLOBAL_MAX}/day")
print(f"   ALT_TEXT: {ALT_TEXT}")
print(f"   VARIANT_ENABLE: {VARIANT_ENABLE}")
print(f"   PROMPT_UNIQUIFIER: {PROMPT_UNIQUIFIER}")
if PROMPT_UNIQUIFIER:
    print(f"   Session token: #{session_token}")
print(f"   PROCESSED_STATE_FILE: {PROCESSED_STATE_FILE}")
print(f"   PROCESSED_STATE_CAP: {PROCESSED_STATE_CAP}")

def load_last_id():
    try:
        with open(LAST_ID_FILE, "r") as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def save_last_id(tid) -> None:
    with open(LAST_ID_FILE, "w") as f:
        f.write(str(tid))


def load_processed_ids():
    """Load processed tweet IDs from local state file."""
    global processed_tweet_ids
    try:
        with open(PROCESSED_STATE_FILE, "r") as f:
            ids = [line.strip() for line in f if line.strip()]
            processed_tweet_ids = set(ids[-PROCESSED_STATE_CAP:])
            print(f"📂 Loaded {len(processed_tweet_ids)} processed IDs from {PROCESSED_STATE_FILE}")
    except FileNotFoundError:
        processed_tweet_ids = set()
        print(f"📂 No existing processed state file, starting fresh")


def save_processed_id(tweet_id):
    """Append tweet ID to local state file."""
    processed_tweet_ids.add(str(tweet_id))
    
    # Trim to cap and write
    trimmed = list(processed_tweet_ids)[-PROCESSED_STATE_CAP:]
    try:
        with open(PROCESSED_STATE_FILE, "w") as f:
            f.write("\n".join(trimmed) + "\n")
    except Exception as e:
        print(f"⚠️ Failed to save processed ID {tweet_id}: {e}")


def reset_rate_limits_if_needed():
    """Reset rate limit counters if it's a new day."""
    global rate_limit_reset_date, user_reply_counts, global_reply_count
    today = datetime.now().date()
    if today > rate_limit_reset_date:
        print(f"📅 New day detected, resetting rate limits")
        user_reply_counts.clear()
        global_reply_count = 0
        rate_limit_reset_date = today


def check_rate_limits(username):
    """Check if rate limits allow processing. Returns (can_process, reason)."""
    reset_rate_limits_if_needed()
    
    # Check global cap
    if GLOBAL_MAX > 0 and global_reply_count >= GLOBAL_MAX:
        return False, f"global daily limit ({GLOBAL_MAX}) reached"
    
    # Check per-user cap
    if PER_USER_MAX > 0 and user_reply_counts[username] >= PER_USER_MAX:
        return False, f"per-user daily limit ({PER_USER_MAX}) for @{username} reached"
    
    return True, ""


def increment_rate_limits(username):
    """Increment rate limit counters after successful reply."""
    global global_reply_count
    user_reply_counts[username] += 1
    global_reply_count += 1


def fetch_mentions(since_id=None):
    query = f"@{BOT_HANDLE} -is:retweet -from:{BOT_HANDLE}"
    return client.search_recent_tweets(
        query=query,
        since_id=since_id,
        tweet_fields="id,author_id,attachments,created_at,entities",
        expansions="author_id,attachments.media_keys",
        media_fields="url,type,preview_image_url",
        user_fields="username,profile_image_url",
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
    usernames = {}
    for u in users:
        usernames[str(u.id)] = u.username
        # Cache profile images if available
        profile_url = getattr(u, "profile_image_url", None)
        if profile_url:
            user_profile_cache[u.username] = enhance_profile_image_url(profile_url)
    return usernames


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


def enhance_profile_image_url(profile_url):
    """Replace _normal. with _400x400. for higher resolution avatar."""
    if "_normal." in profile_url:
        return profile_url.replace("_normal.", "_400x400.")
    return profile_url


def extract_mentioned_users(tweet):
    """Extract mentioned usernames from tweet entities, excluding bot and author."""
    entities = getattr(tweet, "entities", None)
    if not entities:
        return []
    
    mentions = []
    if isinstance(entities, dict):
        user_mentions = entities.get("mentions", []) or []
    else:
        user_mentions = getattr(entities, "mentions", []) or []
    
    for mention in user_mentions:
        username = getattr(mention, "username", None) or mention.get("username", None)
        if username and username.lower() not in [BOT_HANDLE.lower()]:
            mentions.append(username)
    
    return mentions


def resolve_user_profile_image(username):
    """Resolve a user's profile image URL, using cache or API call."""
    if username in user_profile_cache:
        return user_profile_cache[username]
    
    try:
        user = client.get_user(username=username, user_fields="profile_image_url")
        if user.data:
            profile_url = getattr(user.data, "profile_image_url", None)
            if profile_url:
                enhanced_url = enhance_profile_image_url(profile_url)
                user_profile_cache[username] = enhanced_url
                return enhanced_url
    except Exception as e:
        print(f"⚠️ Failed to resolve profile for @{username}: {e}")
    
    return None


def determine_person_image_url(tweet, usernames, media_map):
    """Determine the person image URL based on priority order."""
    # 1. First check for attached photo (existing behavior)
    photo_url = first_photo_url(tweet, media_map)
    if photo_url:
        print(f"📷 Using attached_photo: {photo_url}")
        return photo_url
    
    # 2. Check for mentioned users (excluding bot and author)
    author_username = usernames.get(str(tweet.author_id), "")
    mentioned_users = extract_mentioned_users(tweet)
    
    # Filter out the author from mentions
    other_mentions = [u for u in mentioned_users if u.lower() != author_username.lower()]
    
    # 3. If other users mentioned, use right-most (last) resolvable one
    for username in reversed(other_mentions):
        profile_url = resolve_user_profile_image(username)
        if profile_url:
            print(f"👤 Using mentioned_user:@{username}: {profile_url}")
            return profile_url
    
    # 4. Fallback to author's profile image
    if author_username:
        profile_url = resolve_user_profile_image(author_username)
        if profile_url:
            print(f"🙋 Using author_avatar:@{author_username}: {profile_url}")
            return profile_url
    
    return None


def preload_liked_tweets():
    """Preload recent liked tweet IDs for the bot user."""
    global bot_user_id, liked_tweet_ids
    
    if not SKIP_IF_LIKED:
        return
    
    try:
        # Get bot's user ID and username
        me = client.get_me(user_auth=True)
        if me.data:
            bot_user_id = me.data.id
            bot_username = getattr(me.data, 'username', 'unknown')
            print(f"🤖 Bot user ID: {bot_user_id}, username: @{bot_username}")
            
            # Check if BOT_HANDLE matches authenticated account
            if bot_username.lower() != BOT_HANDLE.lower():
                print(f"⚠️ Warning: BOT_HANDLE ({BOT_HANDLE}) doesn't match authenticated account (@{bot_username}). Skipping will only respect likes from @{bot_username}.")
        else:
            print("⚠️ Could not determine bot user ID")
            return
        
        # Preload liked tweets
        liked_tweets = []
        pagination_token = None
        
        while len(liked_tweets) < LIKED_PRELOAD_LIMIT:
            try:
                response = client.get_liked_tweets(
                    id=bot_user_id,
                    max_results=min(100, LIKED_PRELOAD_LIMIT - len(liked_tweets)),
                    pagination_token=pagination_token,
                    tweet_fields="id",
                    user_auth=True
                )
                
                if not response.data:
                    break
                
                liked_tweets.extend(response.data)
                
                # Check for more pages
                next_token = None
                if hasattr(response, 'meta'):
                    if isinstance(response.meta, dict):
                        next_token = response.meta.get("next_token")
                    else:
                        next_token = getattr(response.meta, "next_token", None)
                
                if next_token:
                    pagination_token = next_token
                else:
                    break
                    
            except Exception as e:
                print(f"⚠️ Error fetching liked tweets: {e}")
                break
        
        # Store tweet IDs in set as strings
        liked_tweet_ids = {str(tweet.id) for tweet in liked_tweets}
        print(f"📋 Preloaded {len(liked_tweet_ids)} liked tweet IDs")
        
    except Exception as e:
        print(f"⚠️ Failed to preload liked tweets: {e}")


def mark_tweet_as_processed(tweet_id):
    """Like the tweet to mark it as processed (based on LIKE_MODE)."""
    should_like = False
    
    if LIKE_MODE == "all":
        should_like = True
    elif LIKE_MODE == "probabilistic":
        should_like = random.random() < LIKE_PROB
    # LIKE_MODE == "none" means should_like stays False
    
    if should_like:
        try:
            client.like(tweet_id, user_auth=True)
            liked_tweet_ids.add(str(tweet_id))
            print(f"❤️ Liked tweet {tweet_id}")
        except Exception as e:
            print(f"⚠️ Failed to like tweet {tweet_id}: {e}")
    else:
        print(f"💭 Not liking tweet {tweet_id} (LIKE_MODE={LIKE_MODE})")


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
    # Add prompt uniquification if enabled
    if PROMPT_UNIQUIFIER:
        prompt = f"{prompt}\n#session:{session_token}"
    
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


def apply_image_variation(image_path: str) -> str:
    """Apply subtle variation to image if VARIANT_ENABLE and Pillow available."""
    if not VARIANT_ENABLE:
        return image_path
    
    try:
        from PIL import Image
        import numpy as np
        
        img = Image.open(image_path)
        
        # Subtle brightness jitter (±0.5%)
        if img.mode in ("RGB", "RGBA"):
            arr = np.array(img, dtype=np.float32)
            brightness_factor = 1.0 + random.uniform(-0.005, 0.005)
            arr = np.clip(arr * brightness_factor, 0, 255).astype(np.uint8)
            img = Image.fromarray(arr, mode=img.mode)
        
        # Single-pixel alpha nudge (if RGBA)
        if img.mode == "RGBA":
            pixels = img.load()
            width, height = img.size
            x, y = random.randint(0, width - 1), random.randint(0, height - 1)
            r, g, b, a = pixels[x, y]
            pixels[x, y] = (r, g, b, min(255, a + random.randint(0, 1)))
        
        # Save variant
        variant_path = image_path.replace(".png", "_v.png")
        img.save(variant_path, "PNG")
        print(f"🎨 Applied subtle variation -> {variant_path}")
        return variant_path
    except ImportError:
        print(f"⚠️ Pillow not available, skipping variation")
        return image_path
    except Exception as e:
        print(f"⚠️ Variation failed: {e}, using original")
        return image_path



def upload_media(path: str) -> str:
    """Upload media and optionally add alt text."""
    media = api_v1.media_upload(filename=path)
    media_id = str(media.media_id)
    
    # Add alt text if enabled
    if ALT_TEXT:
        try:
            alt_text = "Profile image edited with stylish sunglasses and vibrant gradient background"
            api_v1.create_media_metadata(media_id, alt_text)
            print(f"📝 Added alt text to media {media_id}")
        except Exception as e:
            print(f"⚠️ Failed to add alt text: {e}")
    
    return media_id


def reply_with_media(in_reply_to_tweet_id: str, media_id: str, username: str):
    """
    Try multiple Tweepy signatures for compatibility.
    1. Older v2 style: media_ids=[...] (no 'media' dict)
    2. Newer style (if available): reply={}, media={}
    3. v1.1 fallback via api_v1.update_status
    """
    # Use random reply variant
    text = random.choice(REPLY_VARIANTS).format(username=username)

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


def process_tweet(tweet, usernames, media_map):
    tweet_id_str = str(tweet.id)
    author_username = usernames.get(str(tweet.author_id), "")
    
    # Skip if tweet is authored by the bot to prevent self-recursion
    if author_username.lower() == BOT_HANDLE.lower():
        print(f"🔄 Skipping {tweet.id}: tweet authored by bot (@{author_username}) - avoiding self-recursion")
        save_processed_id(tweet_id_str)  # Mark as processed to avoid re-queuing
        return
    
    # Check 1: Local processed state (primary dedupe)
    if tweet_id_str in processed_tweet_ids:
        print(f"⏩ Skipping {tweet.id}: already in local processed state")
        return
    
    # Check 2: Liked set (for backward compatibility with SKIP_IF_LIKED)
    if SKIP_IF_LIKED and tweet_id_str in liked_tweet_ids:
        print(f"⏩ Skipping {tweet.id}: already processed (liked)")
        save_processed_id(tweet_id_str)  # Sync to local state
        return
    
    # Check 3: Rate limits
    can_process, reason = check_rate_limits(author_username)
    if not can_process:
        print(f"🚫 Skipping {tweet.id}: {reason}")
        save_processed_id(tweet_id_str)  # Mark as processed to prevent re-queuing churn
        return
    
    # Determine image source
    person_url = determine_person_image_url(tweet, usernames, media_map)
    if not person_url:
        has_attachments = bool(getattr(tweet, "attachments", None))
        print(f"⏭️  {tweet.id}: no usable image source (attachments={has_attachments}); skipping.")
        save_processed_id(tweet_id_str)  # Mark as processed
        return
    if not SUNGLASSES_URL or not BACKGROUND_URL:
        print("❗ Set SUNGLASSES_URL and BACKGROUND_URL in your environment")
        return

    # Humanization: random delay before processing
    if HUMANIZE_DELAY:
        delay = random.uniform(REPLY_MIN_DELAY, REPLY_MAX_DELAY)
        print(f"⏱️ Waiting {delay:.1f}s before replying to {tweet.id}")
        time.sleep(delay)

    # Generate image
    out_path = run_nano_banana(person_url, SUNGLASSES_URL, BACKGROUND_URL, NANO_PROMPT)
    variant_path = None
    try:
        # Apply variation if enabled
        final_path = apply_image_variation(out_path)
        if final_path != out_path:
            variant_path = final_path
        
        # Upload and reply
        media_id = upload_media(final_path)
        handle = usernames.get(str(tweet.author_id), "")
        reply_with_media(str(tweet.id), media_id, handle)
        print(f"✅ Replied to {tweet.id} (@{handle})")
        
        # Mark as processed (local state + optional like)
        save_processed_id(tweet_id_str)
        mark_tweet_as_processed(tweet.id)
        
        # Increment rate limits
        increment_rate_limits(author_username)
        
    finally:
        # Cleanup temp files
        try:
            os.remove(out_path)
            if variant_path and variant_path != out_path:
                os.remove(variant_path)
        except Exception:
            pass


def main():
    # Load local processed state
    load_processed_ids()
    
    # Preload liked tweets (for backward compat with SKIP_IF_LIKED)
    preload_liked_tweets()
    
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
        
        # Add jitter to poll interval
        jitter = random.uniform(0, POLL_JITTER_MAX)
        sleep_time = POLL_SECONDS + jitter
        print(f"😴 Sleeping {sleep_time:.1f}s (base={POLL_SECONDS}s + jitter={jitter:.1f}s)")
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()
