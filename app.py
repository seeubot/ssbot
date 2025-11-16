import os
import json
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS 
from pymongo import MongoClient, ReturnDocument 
from bson import ObjectId
from datetime import datetime
import logging
import time
from functools import wraps
import threading
from cachetools import TTLCache
import pymongo.operations 
import urllib.parse

# --- CONSTANTS & CONFIGURATION ---
# NOTE: These IDs must be set in your environment variables for production use.
ADMIN_TELEGRAM_ID = 1352497419
GROUP_TELEGRAM_ID = -1002541647242
CONTENT_FORWARD_CHANNEL_ID = -1002776780769
PRODUCT_NAME = "Adult-Hub"
ACCESS_URL = "teluguxx.vercel.app"

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- MONGODB SETUP ---
client = None
db = None
content_collection = None
counter_collection = None

def init_mongodb():
    global client, db, content_collection, counter_collection
    
    try:
        MONGODB_URI = os.environ.get("MONGODB_URI")
        if not MONGODB_URI:
            logger.error("MONGODB_URI environment variable is not set.")
            return False
        
        # Use a non-global client connection to ensure thread safety if needed later, 
        # but for now, we rely on the main thread setup
        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
        client.admin.command('ping')
        
        db_name = os.environ.get("DB_NAME", "streamhub")
        collection_name = os.environ.get("COLLECTION_NAME", "content_items")
        
        db = client[db_name]
        content_collection = db[collection_name]
        counter_collection = db["counters"]
        
        # Ensure indexes
        content_collection.create_index([("created_at", -1)])
        content_collection.create_index([("tags", 1)])
        content_collection.create_index([("views", -1)])
        
        logger.info(f"MongoDB connected successfully. Database: {db_name}")
        return True
        
    except Exception as e:
        logger.error(f"MongoDB initialization failed: {e}")
        content_collection = None
        client = None
        db = None
        return False

def get_next_sequence_value(sequence_name):
    if counter_collection is None:
        return 0
    try:
        result = counter_collection.find_one_and_update(
            {'_id': sequence_name},
            {'$inc': {'sequence_value': 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER
        )
        return result['sequence_value']
    except Exception as e:
        logger.error(f"Error fetching sequence counter: {e}")
        return 0

# --- SIMPLE AUTHENTICATION (RETAINED FOR ADMIN API) ---
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

def require_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != ADMIN_USERNAME or auth.password != ADMIN_PASSWORD:
            return jsonify({"success": False, "error": "Authentication required"}), 401
        return f(*args, **kwargs)
    return decorated_function

# --- CACHING SYSTEM ---
content_cache = TTLCache(maxsize=100, ttl=30)

def get_cache_key():
    path = request.path
    args = sorted(request.args.items())
    return f"{path}?{str(args)}"

def cached_response(timeout=30):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if request.method != 'GET':
                return f(*args, **kwargs)
            
            cache_key = get_cache_key()
            if cache_key in content_cache:
                return content_cache[cache_key]
            
            response = f(*args, **kwargs)
            
            if isinstance(response, tuple) and response[1] == 200:
                content_cache[cache_key] = response
            
            return response
        return decorated_function
    return decorator

# --- VIEW COUNT FUNCTIONALITY ---
view_count_cache = {}
cache_lock = threading.Lock()

def increment_view_count(content_id):
    if content_collection is None:
        return False
    
    try:
        with cache_lock:
            cache_key = f"views_{content_id}"
            view_count_cache[cache_key] = view_count_cache.get(cache_key, 0) + 1
        return True
    except Exception as e:
        logger.error(f"Error incrementing view count: {e}")
        return False

# --- TELEGRAM AND FLASK SETUP ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
APP_URL = os.environ.get("APP_URL")
PORT = int(os.environ.get("PORT", 8000))

# Clean the bot token and create API URL
if BOT_TOKEN:
    BOT_TOKEN = BOT_TOKEN.strip()
    TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}/"
else:
    TELEGRAM_API = None
    logger.warning("BOT_TOKEN environment variable is not set. Telegram features disabled.")

app = Flask(__name__)
CORS(app)

# Global state must be thread-safe or accessed carefully in async context
USER_STATE = {}

# Keyboard template
START_KEYBOARD = {
    'keyboard': [
        [{'text': '/post_diskwala'}, {'text': '/forward_file'}, {'text': '/repost_10'}, {'text': '/files'}],
        [{'text': '/broadcast'}, {'text': '/cancel'}]
    ],
    'resize_keyboard': True,
    'one_time_keyboard': False
}

# --- SIMPLIFIED TELEGRAM FUNCTIONS ---

def send_telegram_request(method, payload):
    """Universal function to send requests to Telegram API with robust error handling"""
    if not TELEGRAM_API:
        logger.warning("Telegram bot token not configured")
        return False
    
    url = TELEGRAM_API + method
    
    # Clean the payload - remove any None values and ensure proper encoding
    clean_payload = {}
    for key, value in payload.items():
        if value is not None:
            if isinstance(value, str):
                # Simple escape for characters that might interfere with JSON or Telegram
                value = value.replace('\\u', '\\\\u') 
            clean_payload[key] = value
    
    logger.info(f"Sending Telegram {method} to chat {clean_payload.get('chat_id')}")
    
    try:
        # This is a BLOCKING network call (slow)
        response = requests.post(url, json=clean_payload, timeout=15)
        
        if response.status_code == 400:
            if 'parse_mode' in clean_payload:
                logger.info(f"Bad request with parse_mode, retrying without...")
                clean_payload.pop('parse_mode', None)
                response = requests.post(url, json=clean_payload, timeout=15)
        
        response.raise_for_status()
        result = response.json()
        
        if result.get('ok'):
            logger.info(f"Telegram {method} successful")
            return result.get('result')
        else:
            logger.error(f"Telegram API error: {result}")
            return False
            
    except requests.exceptions.RequestException as e:
        logger.error(f"Telegram API request failed: {e}")
        return False

def send_message(chat_id, text, reply_markup=None):
    """Send simple text message to Telegram"""
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': None
    }
    
    if reply_markup:
        payload['reply_markup'] = json.dumps(reply_markup)
    
    return send_telegram_request('sendMessage', payload)

# Renamed and updated to handle photo or video media ID
def send_media_post(url, title, media_id, media_type):
    """Send DiskWala link to group using a Telegram File ID (photo or video)."""
    if not TELEGRAM_API or GROUP_TELEGRAM_ID is None:
        return False

    message_text = (
        f"🔥 NEW RELEASE: {title} 🔥\n\n"
        f"Watch Now: {url}\n\n"
        f"Powered by {PRODUCT_NAME}"
    )
    
    method = 'sendPhoto' if media_type == 'photo' else 'sendVideo'
    media_key = 'photo' if media_type == 'photo' else 'video'

    payload = {
        'chat_id': GROUP_TELEGRAM_ID,
        media_key: media_id,
        'caption': message_text
    }
    
    logger.info(f"Sending DiskWala post ({media_type}) to group...")
    success = send_telegram_request(method, payload)
    
    return success

# --- CONTENT MANAGEMENT FUNCTIONS (Contains slow MongoDB I/O) ---

def save_content(content_data):
    """Saves content data with new fields for different posting mechanisms."""
    if content_collection is None: 
        return False
    try:
        # Clean tags and normalize links
        tags = [t.strip().lower() for t in content_data.get('tags', '').split(',') if t.strip()]
        
        document = {
            "title": content_data.get('title'),
            "type": content_data.get('type'),
            "thumbnail_url": content_data.get('thumbnail_url'),
            # Fields for Reposting Logic
            "post_type": content_data.get('post_type'), 
            "telegram_media_id": content_data.get('telegram_media_id'), 
            "telegram_message_id": content_data.get('telegram_message_id'),
            "telegram_chat_id": content_data.get('telegram_chat_id'),
            
            "diskwala_url": content_data.get('diskwala_url'),
            "tags": tags,
            "links": content_data.get('links', []),
            "views": 0,
            "created_at": datetime.utcnow(),
            "last_viewed": datetime.utcnow()
        }
        # BLOCKING MongoDB insert
        result = content_collection.insert_one(document)
        logger.info(f"Content saved with ID: {result.inserted_id}")
        
        content_cache.clear()
        return str(result.inserted_id)
    except Exception as e:
        logger.error(f"MongoDB Save Error: {e}")
        return False

def get_random_content(limit=10):
    """Fetches random content that has a mechanism for reposting."""
    if content_collection is None:
        return []
    try:
        # BLOCKING MongoDB aggregation
        pipeline = [
            {"$match": {"$or": [
                {"telegram_media_id": {"$exists": True, "$ne": None}},
                {"telegram_message_id": {"$exists": True, "$ne": None}},
            ]}},
            {"$sample": {"size": limit}}
        ]
        random_docs = list(content_collection.aggregate(pipeline))
        
        result = []
        for doc in random_docs:
            doc['_id'] = str(doc['_id']) 
            result.append(doc)
        return result
    except Exception as e:
        logger.error(f"Error fetching random content: {e}")
        return []

def repost_single_content(doc):
    """Sends a single content item as a new post to the group, supporting all post types."""
    if GROUP_TELEGRAM_ID is None: return False

    post_type = doc.get('post_type')
    title = doc.get('title', 'Untitled Content')
    
    # REPOST MESSAGE TEXT
    message_text = (
        f"🔄 REPOST: {title}\n\n"
        f"Powered by {PRODUCT_NAME}"
    )
    
    success = False

    if post_type and post_type.startswith('diskwala_'):
        # --- DiskWala/Media Repost (sendPhoto/sendVideo) ---
        diskwala_url = doc.get('diskwala_url')
        media_id = doc.get('telegram_media_id')
        media_type = post_type.split('_')[1] 

        if diskwala_url and media_id:
            message_text += f"\nWatch Now: {diskwala_url}"
            method = 'sendPhoto' if media_type == 'photo' else 'sendVideo'
            media_key = 'photo' if media_type == 'photo' else 'video'
            
            payload = {
                'chat_id': GROUP_TELEGRAM_ID,
                media_key: media_id,
                'caption': message_text
            }
            success = send_telegram_request(method, payload)

    elif post_type == 'forwarded_file':
        # --- Forwarded File Repost (copyMessage) ---
        source_chat_id = doc.get('telegram_chat_id')
        source_message_id = doc.get('telegram_message_id')

        if source_chat_id and source_message_id:
            payload = {
                'chat_id': GROUP_TELEGRAM_ID,
                'from_chat_id': source_chat_id, 
                'message_id': source_message_id,
                'caption': message_text
            }
            success = send_telegram_request('copyMessage', payload)
    
    if not success:
        logger.warning(f"Skipping repost for {doc.get('_id', 'unknown')}: Missing required fields or failed.")
        
    return success

# --- ASYNC PROCESSING FUNCTION (New) ---

def process_telegram_update(update):
    """
    Handles all the synchronous (slow) Telegram logic in a background thread.
    """
    try:
        message = update.get('message')
        if not message:
            return
            
        chat_id = message['chat']['id']
        text = message.get('text', '').strip()
        user_id = message['from']['id']
        
        # Ensure thread-safe access to USER_STATE
        user_state = USER_STATE.get(chat_id, {'step': 'main'})
        
        # Only respond to admin in private chats
        if chat_id > 0 and user_id != ADMIN_TELEGRAM_ID:
            send_message(chat_id, "❌ Access Denied. Only administrator can use this bot.")
            return
        
        # --- DiskWala Multi-step conversation handlers ---

        if user_state['step'] == 'awaiting_diskwala_photo':
            media_id = None
            media_type = None
            
            if 'video' in message:
                media_id = message['video']['file_id']
                media_type = 'video'
            elif 'photo' in message:
                media_id = message['photo'][-1]['file_id']
                media_type = 'photo'

            if media_id:
                USER_STATE[chat_id]['data'] = {
                    'telegram_media_id': media_id, 
                    'media_type': media_type 
                } 
                USER_STATE[chat_id]['step'] = 'awaiting_diskwala_title'
                send_message(chat_id, f"✅ {media_type.title()} saved. Now send the **Title** for the post.")
            else:
                send_message(chat_id, "❌ Please send an **Image or Video Clip** for the thumbnail.")
            return

        elif user_state['step'] == 'awaiting_diskwala_title':
            USER_STATE[chat_id]['data']['title'] = text.strip()
            USER_STATE[chat_id]['step'] = 'awaiting_diskwala_url'
            send_message(chat_id, "✅ Title saved. Now send the **DiskWala URL**.")
            return

        elif user_state['step'] == 'awaiting_diskwala_url':
            diskwala_url = text.strip()
            
            if not diskwala_url.startswith('http'):
                send_message(chat_id, "❌ Please send a valid URL starting with http:// or https://")
                return
            
            title = user_state['data']['title']
            media_id = user_state['data']['telegram_media_id']
            media_type = user_state['data']['media_type']
            
            send_message(chat_id, "⏳ Posting to group and saving to database...")

            # 1. Post to group (SLOW I/O)
            post_result = send_media_post(diskwala_url, title, media_id, media_type)
            
            final_message = ""

            if post_result:
                # 2. Try to save post details to database (SLOW I/O)
                content_data = {
                    "title": title,
                    "type": "video",
                    "thumbnail_url": f"telegram_file_id:{media_id}",
                    "post_type": f"diskwala_{media_type}", 
                    "telegram_media_id": media_id, 
                    "diskwala_url": diskwala_url,
                    "tags": title.lower().split(),
                    "links": [{"url": diskwala_url, "episode_title": "Watch Now"}],
                }
                content_id = save_content(content_data)

                if content_id:
                    final_message = f"🎉 Success! Post '{title}' published and saved with ID: {content_id}."
                else:
                    final_message = f"✅ Post published to group. ❌ **Failed to save to database.** (Post Title: {title})"
                
                # Transition to the continuation state regardless of database save success
                USER_STATE[chat_id]['step'] = 'awaiting_diskwala_continue'
                send_message(chat_id, 
                             final_message + "\n\nDo you want to post **another DiskWala link**? (Reply **Yes** or **No**)",
                             )
            else:
                send_message(chat_id, "❌ Failed to post to Telegram group. Operation finished.", START_KEYBOARD)
                USER_STATE[chat_id] = {'step': 'main'}

            return
        
        elif user_state['step'] == 'awaiting_diskwala_continue':
            if text.lower() in ['yes', 'y']:
                USER_STATE[chat_id] = {'step': 'awaiting_diskwala_photo'}
                send_message(chat_id, "➡️ POST DiskWala: Please send the **Image or Video Clip** for the next post now.")
            elif text.lower() in ['no', 'n', '/cancel']:
                USER_STATE[chat_id] = {'step': 'main'}
                send_message(chat_id, "Returning to main menu. Choose a new action:", START_KEYBOARD)
            else:
                send_message(chat_id, "Please reply **Yes** or **No** to post another DiskWala link.")
            return

            
        # --- File Forwarding Conversation Handler (UPDATED) ---

        elif user_state['step'] == 'awaiting_forward_file':
            is_media = 'photo' in message or 'video' in message or 'document' in message

            if is_media:
                original_message_id = message['message_id']
                
                send_message(chat_id, "⏳ Copying file to content channel (Forward header hidden)...")

                # 1. Use copyMessage to forward without the "Forwarded from" header
                copy_result = send_telegram_request('copyMessage', {
                    'chat_id': CONTENT_FORWARD_CHANNEL_ID,
                    'from_chat_id': chat_id, # Source is admin's private chat
                    'message_id': original_message_id
                })

                if copy_result and copy_result.get('message_id'):
                    channel_message_id = copy_result['message_id']
                    
                    # 2. Save forwarding details (SLOW I/O)
                    title_text = message.get('caption') or f"Forwarded File {channel_message_id}"
                    
                    content_data = {
                        "title": title_text, 
                        "type": "file",
                        "thumbnail_url": "N/A", 
                        "post_type": "forwarded_file",
                        "telegram_message_id": channel_message_id, 
                        "telegram_chat_id": CONTENT_FORWARD_CHANNEL_ID, 
                        "diskwala_url": f"t.me/c/{str(CONTENT_FORWARD_CHANNEL_ID).lstrip('-100')}/{channel_message_id}",
                        "tags": ["forwarded", "file"],
                        "links": [],
                    }
                    content_id = save_content(content_data)

                    final_message = ""
                    if content_id:
                        final_message = f"🎉 Success! File copied to channel (ID: {channel_message_id}) and saved with ID: {content_id}."
                    else:
                        final_message = f"✅ File copied. ❌ **Failed to save to database.**"
                    
                    # Transition to the continuation state
                    USER_STATE[chat_id]['step'] = 'awaiting_file_continue'
                    send_message(chat_id, 
                                final_message + "\n\nDo you want to **forward another file**? (Reply **Yes** or **No**)", 
                                )
                else:
                    send_message(chat_id, "❌ Failed to copy file to content channel. Check bot permissions.", START_KEYBOARD)
                    USER_STATE[chat_id] = {'step': 'main'}
            else:
                send_message(chat_id, "❌ Please send a valid media file (Photo, Video, or Document).")
            return
        
        elif user_state['step'] == 'awaiting_file_continue':
            if text.lower() in ['yes', 'y']:
                USER_STATE[chat_id] = {'step': 'awaiting_forward_file'}
                send_message(chat_id, "➡️ FILE FORWARD: Send the **next Photo, Video, or Document** you want to forward now.")
            elif text.lower() in ['no', 'n', '/cancel']:
                USER_STATE[chat_id] = {'step': 'main'}
                send_message(chat_id, "Returning to main menu. Choose a new action:", START_KEYBOARD)
            else:
                send_message(chat_id, "Please reply **Yes** or **No** to forward another file.")
            return


        # --- Handle Commands ---
        if text.startswith('/start'):
            USER_STATE[chat_id] = {'step': 'main'}
            send_message(chat_id, f"🚀 Welcome to {PRODUCT_NAME} Admin Bot!\n\nUse /post_diskwala for web content, /forward_file for channel files, or /repost_10 to quickly refresh content.", START_KEYBOARD)
            
        elif text.startswith('/post_diskwala'):
            USER_STATE[chat_id] = {'step': 'awaiting_diskwala_photo'}
            send_message(chat_id, "➡️ POST DiskWala: Please send the **Image or Video Clip** for the new post now.")

        elif text.startswith('/forward_file'):
            USER_STATE[chat_id] = {'step': 'awaiting_forward_file'}
            send_message(chat_id, "➡️ FILE FORWARD: Send the **Photo, Video, or Document** you want to save and forward to the Content Channel.")

        elif text.startswith('/repost_10'):
            send_message(chat_id, "⏳ Fetching 10 random content items for reposting (from DiskWala or Forwarded files)...")
            random_items = get_random_content(limit=10) # SLOW I/O
            
            if not random_items:
                send_message(chat_id, "❌ Could not find any content eligible for repost.", START_KEYBOARD)
            else:
                reposted_count = 0
                for item in random_items:
                    if repost_single_content(item): # SLOW I/O
                        reposted_count += 1
                        time.sleep(1) 
                
                send_message(chat_id, f"✅ Repost complete. {reposted_count}/{len(random_items)} items successfully reposted.", START_KEYBOARD)
            
            USER_STATE[chat_id] = {'step': 'main'}

        elif text.startswith('/files'):
            if content_collection is None:
                send_message(chat_id, "❌ Database is currently unavailable.")
                return
                
            try:
                # BLOCKING MongoDB find
                content_cursor = content_collection.find({}, {'title': 1, 'created_at': 1, 'post_type': 1}).sort("created_at", -1).limit(10)
                
                content_list_text = []
                for i, doc in enumerate(content_cursor):
                    title = doc.get('title', 'No Title')
                    _id = str(doc['_id'])
                    post_type = doc.get('post_type', 'N/A')
                    content_list_text.append(f"{i+1}. [{post_type.upper()}] {title} ({_id})")
                    
                if content_list_text:
                    response_text = "📚 Latest 10 Content Items (Title & ID):\n\n" + "\n".join(content_list_text)
                else:
                    response_text = "No content has been uploaded yet."
                
                send_message(chat_id, response_text, START_KEYBOARD)
            except Exception as e:
                logger.error(f"Error fetching files list: {e}")
                send_message(chat_id, "❌ An error occurred while fetching the file list.")
            
        elif text.startswith('/broadcast'):
            USER_STATE[chat_id] = {'step': 'broadcast_message'}
            send_message(chat_id, "➡️ BROADCAST: Send the message you want to broadcast to the group.")
            
        elif text.startswith('/cancel'):
            USER_STATE[chat_id] = {'step': 'main'}
            send_message(chat_id, "❌ Operation cancelled. Choose a new action:", START_KEYBOARD)
            
        # Broadcast Handler
        elif user_state['step'] == 'broadcast_message':
            broadcast_text = text
            send_message(GROUP_TELEGRAM_ID, f"📢 ADMIN ANNOUNCEMENT:\n\n{broadcast_text}") # SLOW I/O
            send_message(chat_id, "✅ Message broadcasted to the group successfully.", START_KEYBOARD)
            USER_STATE[chat_id] = {'step': 'main'}

        else:
            send_message(chat_id, "🤔 I don't recognize that command or state. Use /start to see available commands.")
            
    except Exception as e:
        logger.error(f"Async processing error: {e}")
        # Only try to notify admin if the error is severe enough
        try:
            send_message(ADMIN_TELEGRAM_ID, f"🚨 Critical Processing Error: {e}")
        except Exception:
            pass
        
# --- FLASK ROUTES (The Webhook now only handles immediate response) ---

@app.route('/webhook', methods=['POST'])
def webhook():
    """
    Receives the Telegram update and immediately spins off a thread to process it.
    Returns 200 OK instantly to prevent Telegram timeouts.
    """
    if not BOT_TOKEN:
        return jsonify({"status": "telegram not configured"}), 200
        
    try:
        update = request.get_json(silent=True)
        if not update:
            return jsonify({"status": "no data"}), 200
        
        # Start a new thread for the actual processing (which includes all I/O)
        threading.Thread(target=process_telegram_update, args=(update,)).start()
        
        # Return success immediately
        return jsonify({"status": "received and processing"}), 200
        
    except Exception as e:
        logger.error(f"Webhook error during parsing/threading setup: {e}")
        return jsonify({"status": "error"}), 500


# --- OTHER FLASK ROUTES (RETAINED) ---

@app.route('/', methods=['GET'])
def index():
    return jsonify({
        "service": PRODUCT_NAME, 
        "status": "online",
        "timestamp": datetime.utcnow().isoformat()
    }), 200

@app.route('/health', methods=['GET'])
def health():
    try:
        if client is not None:
            client.admin.command('ping')
            return jsonify({
                "status": "healthy", 
                "database": "connected",
                "timestamp": datetime.utcnow().isoformat()
            }), 200
    except Exception as e:
        logger.error(f"Health check failed: {e}")
    
    return jsonify({"status": "unhealthy", "database": "disconnected"}), 503

@app.route('/api/track-view', methods=['POST'])
def track_view():
    try:
        data = request.get_json(silent=True) or {}
        content_id = data.get('content_id')
        
        if not content_id:
            return jsonify({"success": False, "error": "Content ID required"}), 400
        
        increment_view_count(content_id)
        return jsonify({"success": True, "content_id": content_id, "message": "View count updated"}), 200
            
    except Exception as e:
        logger.error(f"View tracking error: {e}")
        return jsonify({"success": False, "error": "Tracking failed"}), 500

@app.route('/api/content', methods=['GET'])
@cached_response(timeout=30)
def get_content():
    if content_collection is None:
        return jsonify({"error": "Database not configured."}), 503

    try:
        page = max(1, int(request.args.get('page', 1)))
        limit = min(int(request.args.get('limit', 20)), 50)
        skip = (page - 1) * limit
        
        content_type = request.args.get('type')
        tag_filter = request.args.get('tag')
        search_query = request.args.get('q') 
        
        query = {}
        
        if content_type:
            query['type'] = content_type
            
        if tag_filter:
            query['tags'] = tag_filter.lower()
            
        if search_query:
            search_regex = {"$regex": search_query, "$options": "i"}
            query['$or'] = [
                {"title": search_regex},
                {"tags": search_regex},
            ]

        projection = {
            'title': 1, 'type': 1, 'thumbnail_url': 1, 'tags': 1, 
            'views': 1, 'created_at': 1, 'links': 1
        }
        
        total_count = content_collection.count_documents(query)
        
        content_cursor = content_collection.find(
            query, 
            projection
        ).sort("created_at", -1).skip(skip).limit(limit)
        
        content_list = []
        for doc in content_cursor:
            doc['_id'] = str(doc['_id'])
            if 'created_at' in doc:
                doc['created_at'] = doc['created_at'].isoformat()
            content_list.append(doc)
        
        return jsonify({
            "success": True, "data": content_list,
            "pagination": {
                "page": page, "limit": limit, "total": total_count,
                "pages": (total_count + limit - 1) // limit
            }
        }), 200
        
    except Exception as e:
        logger.error(f"API Fetch Error: {e}")
        return jsonify({"success": False, "error": "Failed to retrieve content."}), 500

@app.route('/api/content/<content_id>', methods=['GET'])
@cached_response(timeout=30)
def get_content_by_id(content_id):
    if content_collection is None:
        return jsonify({"error": "Database not configured."}), 503

    try:
        doc = content_collection.find_one({"_id": ObjectId(content_id)})
        if not doc:
            return jsonify({"success": False, "error": "Content not found"}), 404
        
        doc['_id'] = str(doc['_id'])
        if 'created_at' in doc:
            doc['created_at'] = doc['created_at'].isoformat()
        
        return jsonify({"success": True, "data": doc}), 200
        
    except Exception as e:
        logger.error(f"API Single Fetch Error: {e}")
        return jsonify({"success": False, "error": "Invalid content ID"}), 400

@app.route('/api/content/similar/<tags>', methods=['GET'])
@cached_response(timeout=30)
def get_similar_content(tags):
    if content_collection is None:
        return jsonify({"error": "Database not configured."}), 503

    target_tags = [t.strip().lower() for t in tags.split(',') if t.strip()]

    if not target_tags:
        return jsonify({"success": True, "data": []}), 200

    try:
        query = {"tags": {"$in": target_tags}}
        content_cursor = content_collection.find(query).sort("views", -1).limit(10)
        
        content_list = []
        for doc in content_cursor:
            doc['_id'] = str(doc['_id'])
            if 'created_at' in doc:
                doc['created_at'] = doc['created_at'].isoformat()
            content_list.append(doc)
            
        return jsonify({"success": True, "data": content_list}), 200
    except Exception as e:
        logger.error(f"API Similar Fetch Error: {e}")
        return jsonify({"success": False, "error": "Failed to retrieve similar content."}), 500


# --- BACKGROUND TASKS (RETAINED) ---

def flush_view_cache():
    global view_count_cache
    while True:
        time.sleep(30)
        try:
            with cache_lock:
                if not view_count_cache or content_collection is None:
                    continue
                
                bulk_ops = []
                for cache_key, count in list(view_count_cache.items()):
                    if count > 0:
                        content_id = cache_key.replace('views_', '')
                        if ObjectId.is_valid(content_id):
                            bulk_ops.append(
                                pymongo.operations.UpdateOne(
                                    {"_id": ObjectId(content_id)},
                                    {
                                        "$inc": {"views": count},
                                        "$set": {"last_viewed": datetime.utcnow()}
                                    }
                                )
                            )
                
                if bulk_ops:
                    # BLOCKING MongoDB bulk write
                    result = content_collection.bulk_write(bulk_ops, ordered=False)
                    logger.info(f"Flushed {result.modified_count} view count updates")
                    view_count_cache.clear()
                    
        except Exception as e:
            logger.error(f"Error flushing view cache: {e}")

# --- APPLICATION STARTUP (RETAINED) ---

def set_webhook():
    if not APP_URL or not BOT_TOKEN:
        logger.warning("APP_URL or BOT_TOKEN not set. Skipping webhook setup.")
        return False
    
    webhook_url = f"{APP_URL.rstrip('/')}/webhook"
    
    payload = {
        'url': webhook_url,
        'max_connections': 10,
        'drop_pending_updates': True
    }
    
    # BLOCKING network request
    return send_telegram_request('setWebhook', payload)

@app.before_request
def before_request():
    global content_collection
    if content_collection is None:
        init_mongodb()

if __name__ == '__main__':
    logger.info("Starting StreamHub Application...")
    
    if not init_mongodb():
        logger.error("Failed to initialize MongoDB. Continuing without database...")
    
    # Start background tasks
    cache_thread = threading.Thread(target=flush_view_cache, daemon=True)
    cache_thread.start()
    
    if APP_URL and BOT_TOKEN:
        if set_webhook():
            logger.info("Webhook set successfully")
        else:
            logger.error("Failed to set webhook")
    else:
        logger.warning("APP_URL or BOT_TOKEN not set - webhook not configured")
    
    logger.info(f"Starting Flask app on port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)

