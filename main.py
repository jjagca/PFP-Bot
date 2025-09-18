import os
import time
import tempfile
import tweepy
import requests
import replicate
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
    
    # 3. If other users mentioned, use first resolvable one
    for username in other_mentions:
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
        # Get bot's user ID and authenticated username
        me = client.get_me()
        if me.data:
            bot_user_id = me.data.id
            bot_username = me.data.username
            print(f"🤖 Authenticated as @{bot_username} (ID: {bot_user_id})")
            
            # Warn if username doesn't match BOT_HANDLE (case-insensitive)
            if bot_username.lower() != BOT_HANDLE.lower():
                print(f"⚠️ Warning: Authenticated username (@{bot_username}) doesn't match BOT_HANDLE ({BOT_HANDLE})")
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
                    tweet_fields="id"
                )
                
                if not response.data:
                    break
                
                liked_tweets.extend(response.data)
                
                # Check for more pages - support both dict and object forms
                next_token = None
                if hasattr(response, 'meta'):
                    if isinstance(response.meta, dict):
                        next_token = response.meta.get('next_token')
                    else:
                        next_token = getattr(response.meta, 'next_token', None)
                
                if next_token:
                    pagination_token = next_token
                else:
                    break
                    
            except Exception as e:
                print(f"⚠️ Error fetching liked tweets: {e}")
                break
        
        # Store tweet IDs as strings in set
        liked_tweet_ids = {str(tweet.id) for tweet in liked_tweets}
        print(f"📋 Preloaded {len(liked_tweet_ids)} liked tweet IDs")
        
        # Log sample IDs to confirm string typing
        if liked_tweet_ids:
            sample_ids = list(liked_tweet_ids)[:3]
            print(f"📋 Sample IDs (type={type(sample_ids[0]).__name__}, len={len(sample_ids[0])}): {sample_ids}")
        
    except Exception as e:
        print(f"⚠️ Failed to preload liked tweets: {e}")


def mark_tweet_as_processed(tweet_id):
    """Like the tweet to mark it as processed."""
    if not SKIP_IF_LIKED:
        return
    
    try:
        client.like(tweet_id)
        liked_tweet_ids.add(str(tweet_id))
        print(f"❤️ Liked tweet {tweet_id} to mark as processed")
    except Exception as e:
        print(f"⚠️ Failed to like tweet {tweet_id}: {e}")


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


def process_tweet(tweet, usernames, media_map):
    # Skip if already liked (processed) - use string ID comparison
    tweet_id_str = str(tweet.id)
    is_present = tweet_id_str in liked_tweet_ids
    
    if SKIP_IF_LIKED:
        print(f"⏸️  Skip check: id={tweet.id} present={is_present}")
        if is_present:
            print(f"⏩ Skipping {tweet.id}: already processed (liked)")
            return
    
    person_url = determine_person_image_url(tweet, usernames, media_map)
    if not person_url:
        has_attachments = bool(getattr(tweet, "attachments", None))
        print(f"⏭️  {tweet.id}: no usable image source (attachments={has_attachments}); skipping.")
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
        
        # Mark as processed
        mark_tweet_as_processed(tweet.id)
        
    finally:
        try:
            os.remove(out_path)
        except Exception:
            pass


def main():
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
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
